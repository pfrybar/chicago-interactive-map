"""USGS 3DEP lidar: a ground elevation model and roof heights, per 2 km block.

The 2017 Cook County survey is published as an Entwine Point Tile (EPT) octree
on a public S3 bucket. We read only the octree nodes that overlap a block, down
to `lidar.depth` (depth 9 is about 4 points/m²), and cache them under
data/raw/lidar_cook_2017/. Nothing else from the survey is downloaded.

Per block this writes, under data/work/lidar/:
  <i>_<j>.dem.npz       ground elevation (bare earth plus bridge decks), 2 m grid
  <i>_<j>.roofs.parquet lidar statistics for every building whose center is in the block
  <i>_<j>.unmapped.parquet  clusters of building-class points with no footprint
  <i>_<j>.buildings.sha256  fingerprint of the buildings the statistics were made for
"""
import hashlib
import io
import json
import time
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import lru_cache

import geopandas as gpd
import laspy
import numpy as np
import pandas as pd
import requests
import shapely
from pyproj import Transformer
from scipy import ndimage

from .config import RAW, WORK, lidar_source, lidar_work, lidar_workers, load_config, load_sources
from .frame import local_frame
from .grid import BLOCK_LEVEL, Key

# Workers start from a fresh interpreter instead of forking: a fork shares the parent's
# memory copy-on-write, and Python's reference counting soon copies all of it (the
# parent holds every building in the city), multiplying memory by the worker count.
_FRESH = multiprocessing.get_context("forkserver")

LIDAR_RAW = RAW / "lidar_cook_2017"
LIDAR_WORK = lidar_work("lidar")
DEM_CELL = 2.0
MARGIN = 40.0  # meters of extra data around each block, so DEMs agree at block seams

GROUND, NOISE, WATER, BRIDGE, HIGH_NOISE = 2, 7, 9, 17, 18
BUILDING = 6


def cfg() -> dict:
    return load_config()["lidar"]


def datum() -> float:
    """Elevation (NAVD88 m) that maps to local z = 0: street level at State & Madison."""
    return float(load_config()["ground_datum"])


# ---------------------------------------------------------------- EPT access

@lru_cache
def _ept() -> dict:
    path = LIDAR_RAW / "ept.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(load_sources()["lidar_cook_2017"]["url"] + "ept.json", timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
    return json.loads(path.read_text())


def _get(rel: str) -> bytes:
    """Fetch a file from the EPT dataset, using the local cache."""
    path = LIDAR_RAW / rel
    if path.exists():
        return path.read_bytes()
    url = load_sources()["lidar_cook_2017"]["url"] + rel
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.rename(path)
    return r.content


def _node_bounds(key: str) -> tuple[float, float, float, float, int]:
    b = _ept()["bounds"]
    d, x, y, _ = map(int, key.split("-"))
    s = (b[3] - b[0]) / 2 ** d
    return b[0] + x * s, b[1] + y * s, b[0] + (x + 1) * s, b[1] + (y + 1) * s, d


def nodes(bounds_3857, depth: int) -> list[str]:
    """Octree nodes (down to `depth`) overlapping a Web Mercator bounding box."""
    x0, y0, x1, y1 = bounds_3857

    def hits(k):
        a, b, c, d, lv = _node_bounds(k)
        return lv <= depth and a < x1 and c > x0 and b < y1 and d > y0

    out, todo = [], ["0-0-0-0"]
    while todo:
        h = json.loads(_get(f"ept-hierarchy/{todo.pop()}.json"))
        for k, v in h.items():
            if not hits(k):
                continue
            if v == -1:
                todo.append(k)  # this subtree's hierarchy lives in its own file
            elif v > 0:
                out.append(k)
    return out


@lru_cache
def _to_local() -> Transformer:
    return Transformer.from_crs("EPSG:3857", local_frame().crs, always_xy=True)


def read_points(bounds_local, depth: int):
    """Points inside a local-frame box: (x, y, z_local, classification), from the configured survey."""
    if lidar_source() == "cook2022":
        from .lidar2022 import read_points as read_2022
        return read_2022(bounds_local)
    return _read_ept(bounds_local, depth)


def _read_ept(bounds_local, depth: int):
    """2017 points from the EPT octree, read down to `depth`."""
    x0, y0, x1, y1 = bounds_local
    inv = Transformer.from_crs(local_frame().crs, "EPSG:3857", always_xy=True)
    xs, ys = inv.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    keys = nodes((min(xs), min(ys), max(xs), max(ys)), depth)
    with ThreadPoolExecutor(8) as ex:
        blobs = list(ex.map(lambda k: _get(f"ept-data/{k}.laz"), keys))
    parts = []
    for blob in blobs:
        las = laspy.read(io.BytesIO(blob))
        x, y = _to_local().transform(np.asarray(las.x), np.asarray(las.y))
        m = (x >= x0) & (x < x1) & (y >= y0) & (y < y1)
        if m.any():
            parts.append((x[m], y[m], np.asarray(las.z)[m] - datum(), np.asarray(las.classification)[m]))
    if not parts:
        e = np.empty(0)
        return e, e, e, np.empty(0, np.uint8)
    return tuple(np.concatenate(p) for p in zip(*parts))


# ---------------------------------------------------------------- products

class Dem:
    """Ground elevation on a regular grid, in local meters."""

    def __init__(self, z: np.ndarray, x0: float, y0: float, cell: float):
        self.z, self.x0, self.y0, self.cell = z, x0, y0, cell

    def sample(self, x, y) -> np.ndarray:
        """Bilinear sample at local (x, y); clamps to the grid edge."""
        fx = np.clip((np.asarray(x) - self.x0) / self.cell - 0.5, 0, self.z.shape[1] - 1.001)
        fy = np.clip((np.asarray(y) - self.y0) / self.cell - 0.5, 0, self.z.shape[0] - 1.001)
        i, j = fy.astype(int), fx.astype(int)
        ty, tx = fy - i, fx - j
        z = self.z.astype(np.float32)
        return (z[i, j] * (1 - tx) * (1 - ty) + z[i, j + 1] * tx * (1 - ty)
                + z[i + 1, j] * (1 - tx) * ty + z[i + 1, j + 1] * tx * ty)

    def window(self, bounds) -> np.ndarray:
        x0, y0, x1, y1 = bounds
        j0, j1 = int((x0 - self.x0) // self.cell), int(np.ceil((x1 - self.x0) / self.cell))
        i0, i1 = int((y0 - self.y0) // self.cell), int(np.ceil((y1 - self.y0) / self.cell))
        h, w = self.z.shape
        return self.z[max(i0, 0):min(i1, h), max(j0, 0):min(j1, w)]

    def save(self, path) -> None:
        np.savez_compressed(path, z=self.z.astype(np.float16), x0=self.x0, y0=self.y0, cell=self.cell)

    @classmethod
    def load(cls, path) -> "Dem":
        d = np.load(path)
        return cls(d["z"].astype(np.float32), float(d["x0"]), float(d["y0"]), float(d["cell"]))


def build_dem(x, y, z, cls_, bounds, footprints=None, road_bridges=None) -> Dem:
    """Mean of ground and bridge-deck points per 2 m cell; gaps (buildings, water) take the nearest value.

    Ground-class points inside building footprints are dropped: some low roofs are
    classified as ground, which would put mounds under buildings. Bridge-deck points
    count only on mapped road and path bridges; the class also covers elevated L
    tracks and viaducts, which aren't ground.
    """
    x0, y0, x1, y1 = bounds
    w, h = int(round((x1 - x0) / DEM_CELL)), int(round((y1 - y0) / DEM_CELL))
    m = cls_ == GROUND
    deck = np.flatnonzero(cls_ == BRIDGE)
    if road_bridges is not None and not road_bridges.is_empty and len(deck):
        on = shapely.contains_xy(road_bridges, x[deck], y[deck])
        m[deck[on]] = True
    if footprints is not None and len(footprints) and m.any():
        idx = np.flatnonzero(m)
        tree = shapely.STRtree(shapely.buffer(footprints, 0.5))
        inside = tree.query(shapely.points(x[idx], y[idx]), predicate="within")[0]
        m[idx[np.unique(inside)]] = False
    j = np.clip(((x[m] - x0) / DEM_CELL).astype(int), 0, w - 1)
    i = np.clip(((y[m] - y0) / DEM_CELL).astype(int), 0, h - 1)
    idx = i * w + j
    total = np.bincount(idx, weights=z[m], minlength=w * h)
    count = np.bincount(idx, minlength=w * h)
    grid = np.full(w * h, np.nan, np.float32)
    has = count > 0
    grid[has] = total[has] / count[has]
    grid = grid.reshape(h, w)
    missing = np.isnan(grid)
    if missing.all():
        return Dem(np.zeros((h, w), np.float32), x0, y0, DEM_CELL)
    if missing.any():
        _, (ii, jj) = ndimage.distance_transform_edt(missing, return_indices=True)
        grid = grid[ii, jj]
    # Light smoothing: curbs and stray points make the 2 m grid stepped.
    grid = ndimage.gaussian_filter(grid, sigma=1.5, mode="nearest")
    return Dem(grid.astype(np.float32), x0, y0, DEM_CELL)


def roof_stats(buildings: gpd.GeoDataFrame, x, y, z, cls_, dem: Dem) -> pd.DataFrame:
    """Lidar points inside each footprint (shrunk 0.5 m to skip walls and neighbors)."""
    if not len(buildings):
        return pd.DataFrame()
    above = (cls_ != GROUND) & (cls_ != NOISE) & (cls_ != HIGH_NOISE) & (cls_ != WATER) & (cls_ != BRIDGE)
    gz = dem.sample(x, y)
    above &= z > gz + 1.5
    px, py, pz, pc = x[above], y[above], z[above], cls_[above]

    shrunk = shapely.buffer(buildings.geometry.values, -0.5)
    empty = shapely.is_empty(shrunk)
    shrunk[empty] = buildings.geometry.values[empty]
    tree = shapely.STRtree(shrunk)
    pt_idx, b_idx = tree.query(shapely.points(px, py), predicate="within")
    # Where footprints overlap (an outline around several towers, parts inside parts),
    # a point counts only toward the smallest footprint containing it.
    area = shapely.area(shrunk)
    order = np.lexsort((area[b_idx], pt_idx))
    pt_idx, b_idx = pt_idx[order], b_idx[order]
    first = np.r_[True, pt_idx[1:] != pt_idx[:-1]]
    pt_idx, b_idx = pt_idx[first], b_idx[first]
    df = pd.DataFrame({"b": b_idx, "z": pz[pt_idx], "c": pc[pt_idx]})

    out = []
    groups = dict(tuple(df.groupby("b"))) if len(df) else {}
    base = _footprint_ground(buildings, dem)
    for k, row in enumerate(buildings.itertuples()):
        rec = {"id": row.id, "base_min": base[k][0], "base_med": base[k][1], "n": 0}
        g = groups.get(k)
        if g is not None and len(g):
            bld = g[g["c"] == BUILDING]
            # Classes are unreliable downtown (rooftops classed as vegetation), but in
            # neighborhoods trees overhang houses. Trust building-class points when they dominate.
            use = bld if len(bld) >= 0.3 * len(g) else g
            zs = use["z"].values
            rec.update(n=len(g), n_used=len(use), classes="building" if use is bld else "all",
                       p50=float(np.percentile(zs, 50)), p75=float(np.percentile(zs, 75)),
                       p90=float(np.percentile(zs, 90)), p95=float(np.percentile(zs, 95)),
                       p98=float(np.percentile(zs, 98)))
        out.append(rec)
    return pd.DataFrame(out)


def _footprint_ground(buildings, dem: Dem) -> list[tuple[float, float]]:
    out = []
    for geom in buildings.geometry.values:
        win_pts = np.asarray(shapely.get_coordinates(geom))
        c = shapely.point_on_surface(geom)
        pts = np.vstack([win_pts, shapely.get_coordinates(c)])
        g = dem.sample(pts[:, 0], pts[:, 1])
        out.append((float(g.min()), float(np.median(g))))
    return out


def unmapped(buildings, x, y, cls_, bounds, min_area: float = 40.0) -> pd.DataFrame:
    """Clusters of building-class points that fall outside every footprint."""
    m = cls_ == BUILDING
    if not m.any():
        return pd.DataFrame(columns=["x", "y", "area"])
    bx, by = x[m], y[m]
    if len(buildings):
        tree = shapely.STRtree(shapely.buffer(buildings.geometry.values, 1.5))
        inside = tree.query(shapely.points(bx, by), predicate="within")[0]
        keep = np.ones(len(bx), bool)
        keep[inside] = False
        bx, by = bx[keep], by[keep]
    x0, y0, x1, y1 = bounds
    w, h = int((x1 - x0) / DEM_CELL), int((y1 - y0) / DEM_CELL)
    j = np.clip(((bx - x0) / DEM_CELL).astype(int), 0, w - 1)
    i = np.clip(((by - y0) / DEM_CELL).astype(int), 0, h - 1)
    grid = np.zeros((h, w), np.int32)
    np.add.at(grid, (i, j), 1)
    labels, n = ndimage.label(grid >= 2)
    rows = []
    for lab, sl in enumerate(ndimage.find_objects(labels), 1):
        cells = (labels[sl] == lab).sum()
        area = cells * DEM_CELL ** 2
        if area < min_area:
            continue
        ci, cj = ndimage.center_of_mass(labels[sl] == lab)
        rows.append({"x": x0 + (sl[1].start + cj + 0.5) * DEM_CELL,
                     "y": y0 + (sl[0].start + ci + 0.5) * DEM_CELL, "area": area})
    return pd.DataFrame(rows, columns=["x", "y", "area"])


# ---------------------------------------------------------------- per block

def block_paths(key: Key):
    stem = f"{key.i}_{key.j}"
    return (LIDAR_WORK / f"{stem}.dem.npz", LIDAR_WORK / f"{stem}.roofs.parquet",
            LIDAR_WORK / f"{stem}.unmapped.parquet")


def fingerprint(buildings: gpd.GeoDataFrame) -> str:
    """Hash of a block's building ids and footprints.

    Cached block outputs are only valid for the buildings they were made from: when
    a building rule changes which outlines exist or their shapes, the block reruns.
    """
    h = hashlib.sha256()
    for bid, wkb in sorted(zip(buildings["id"].astype(str), shapely.to_wkb(buildings.geometry.values))):
        h.update(bid.encode() + b"\0" + wkb)
    return h.hexdigest()


def is_current(stamp, outputs, fp: str) -> bool:
    return all(p.exists() for p in outputs) and stamp.exists() and stamp.read_text() == fp


def _stamp(key: Key):
    return LIDAR_WORK / f"{key.i}_{key.j}.buildings.sha256"


def _block_job(args):
    key, bpath, depth, fp = args
    t0 = time.time()
    x0, y0, x1, y1 = key.bounds
    grown = (x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN)
    x, y, z, c = read_points(grown, depth)
    from .osm import load_features
    feats = load_features(grown)
    is_bldg = (feats["building"].notna() | feats["building:part"].notna()) & feats["location"].isna()
    footprints = feats[is_bldg & feats.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry.values
    from .ground import PATH_WIDTH, ROAD_WIDTH
    hw = feats["highway"]
    on_bridge = (feats["bridge"].notna() & (feats["bridge"] != "no") & (feats.geometry.geom_type == "LineString")
                 & (hw.isin(ROAD_WIDTH.keys()) | hw.isin(PATH_WIDTH.keys())))
    road_bridges = shapely.union_all(feats[on_bridge].geometry.buffer(12, cap_style="flat").values) \
        if on_bridge.any() else None
    if road_bridges is not None:
        shapely.prepare(road_bridges)
    dem_full = build_dem(x, y, z, c, grown, footprints, road_bridges)
    m = int(MARGIN / DEM_CELL)
    dem = Dem(dem_full.z[m:-m, m:-m], x0, y0, DEM_CELL)
    buildings = gpd.read_parquet(bpath) if bpath else gpd.GeoDataFrame({"id": []}, geometry=[])
    roofs = roof_stats(buildings, x, y, z, c, dem_full)
    gaps = unmapped(buildings, x, y, c, key.bounds)
    dem_path, roofs_path, gaps_path = block_paths(key)
    dem.save(dem_path)
    roofs.to_parquet(roofs_path)
    gaps.to_parquet(gaps_path)
    _stamp(key).write_text(fp)
    return key, len(x), time.time() - t0


def run(blocks: dict, buildings: gpd.GeoDataFrame, force: bool = False) -> None:
    """Process every block in `blocks` without current outputs (or all, with force)."""
    LIDAR_WORK.mkdir(parents=True, exist_ok=True)
    if lidar_source() == "cook2022":
        from .lidar2022 import fetch_for
        fetch_for(blocks)
    depth = cfg()["depth"]
    tmp = WORK / "lidar-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    from .grid import index as grid_index
    bi, bj = grid_index(buildings["cx"].values, buildings["cy"].values, BLOCK_LEVEL)
    jobs = []
    for key in sorted(blocks):
        sel = buildings[(bi == key.i) & (bj == key.j)][["id", "geometry"]]
        fp = fingerprint(sel)
        if not force and is_current(_stamp(key), block_paths(key), fp):
            continue
        path = None
        if len(sel):
            path = tmp / f"{key.i}_{key.j}.parquet"
            sel.to_parquet(path)
        jobs.append((key, path, depth, fp))
    if not jobs:
        print("  lidar: all blocks up to date")
        return
    workers = lidar_workers(cfg()["workers"])
    print(f"  lidar: {len(jobs)} blocks to process at depth {depth}, {workers} workers")
    t0 = time.time()
    with ProcessPoolExecutor(mp_context=_FRESH, max_workers=workers) as pool:
        for n, (key, npts, secs) in enumerate(pool.map(_block_job, jobs), 1):
            if n % 5 == 0 or n == len(jobs):
                cached = sum(f.stat().st_size for f in (LIDAR_RAW / "ept-data").glob("*.laz")) / 1e9
                print(f"    {n}/{len(jobs)} blocks, {time.time() - t0:.0f}s, cache {cached:.1f} GB "
                      f"(last: {npts / 1e6:.1f}M points in {secs:.0f}s)")


def load_dem(key: Key) -> Dem | None:
    p = block_paths(key)[0]
    return Dem.load(p) if p.exists() else None


def record_lock() -> None:
    """Record what was read from the survey in sources.lock.json (only when it changed)."""
    import hashlib
    from datetime import datetime, timezone
    from .config import LOCK_FILE
    lock = json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else {}
    if lidar_source() == "cook2022":
        from .lidar2022 import RAW_2022
        name, files = "lidar_cook_2022", sorted(RAW_2022.glob("*.laz"))
        entry = {"url": load_sources()[name]["url"], "density": cfg()["cook2022_density"],
                 "tiles_cached": len(files), "bytes": sum(f.stat().st_size for f in files)}
    else:
        name, files = "lidar_cook_2017", list((LIDAR_RAW / "ept-data").glob("*.laz"))
        entry = {"url": load_sources()[name]["url"],
                 "ept_json_sha256": hashlib.sha256((LIDAR_RAW / "ept.json").read_bytes()).hexdigest(),
                 "depth": cfg()["depth"], "nodes_cached": len(files), "bytes": sum(f.stat().st_size for f in files)}
    old = {k: v for k, v in lock.get(name, {}).items() if k not in ("retrieved_at", "license")}
    if old == entry:
        return
    lock[name] = entry | {"retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "license": load_sources()[name]["license"]}
    LOCK_FILE.write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")
