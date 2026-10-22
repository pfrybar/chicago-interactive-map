"""Trees from the lidar: positions, heights and crown sizes, per 2 km block.

Vegetation points (classes 3–5) outside building footprints make a 1 m canopy
height model; its local peaks are tree tops. (Downtown, many roofs are classed as
vegetation, hence the footprint mask.) Crown radius comes from height with a
simple allometric rule; species aren't known, since Chicago has no openly
licensed tree inventory.

Writes data/work/trees/<i>_<j>.parquet: x, y, base (ground z), height, radius.
"""
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
import shapely
from scipy import ndimage

from . import mesh
from .config import lidar_source, lidar_work, lidar_workers, load_config
from .grid import Key
from .lidar import block_paths, load_dem, read_points
from .osm import load_features

_FRESH = multiprocessing.get_context("forkserver")

TREES = lidar_work("trees")
VEGETATION = (3, 4, 5)
CELL = 1.0
MIN_HEIGHT = 3.0
MIN_SUPPORT = 6  # vegetation points in the 3 m window around a top
# A tree top needs a crown: at least this many 1 m cells within 3 m of the peak at half
# its height or more. Light poles, signals and strips of facade points along buildings
# have a sharp peak with nothing around it. Measured against the 2025 orthophotos on
# four Loop tiles: detections with smaller crowns showed no vegetation 85% of the time,
# and the rule drops 26% of detections while losing about 10% of the trees that pass.
# Taller trees have wider crowns, so above TALL_M a top needs MIN_CROWN_CELLS_TALL: tall
# detections with narrow tops failed 70-100% of the time (mostly points on buildings).
MIN_CROWN_CELLS = 13
MIN_CROWN_CELLS_TALL = 30
TALL_M = 8.0


def detect(key: Key) -> pd.DataFrame:
    x0, y0, x1, y1 = key.bounds
    m = 10.0
    grown = (x0 - m, y0 - m, x1 + m, y1 + m)
    dem = load_dem(key)
    x, y, z, c = read_points(grown, load_config()["lidar"]["depth"])
    veg = np.isin(c, VEGETATION)
    x, y, z = x[veg], y[veg], z[veg]
    h = z - dem.sample(x, y)
    keep = (h > 2.0) & (h < 40.0)
    x, y, h = x[keep], y[keep], h[keep]

    feats = load_features(grown)
    is_bldg = feats["building"].notna() | feats["building:part"].notna()
    fps = feats[is_bldg & feats.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry.values
    if len(fps) and len(x):
        tree = shapely.STRtree(shapely.buffer(fps, 1.0))
        inside = np.unique(tree.query(shapely.points(x, y), predicate="within")[0])
        out = np.ones(len(x), bool)
        out[inside] = False
        x, y, h = x[out], y[out], h[out]
    if not len(x):
        return pd.DataFrame(columns=["x", "y", "base", "height", "radius", "crown_cells", "survey"])

    gx0, gy0 = grown[0], grown[1]
    w, hh = int(np.ceil((grown[2] - gx0) / CELL)), int(np.ceil((grown[3] - gy0) / CELL))
    j = np.clip(((x - gx0) / CELL).astype(int), 0, w - 1)
    i = np.clip(((y - gy0) / CELL).astype(int), 0, hh - 1)
    chm = np.zeros(w * hh, np.float32)
    np.maximum.at(chm, i * w + j, h.astype(np.float32))
    count = np.bincount(i * w + j, minlength=w * hh).reshape(hh, w)
    raw = chm.reshape(hh, w).copy()
    chm = ndimage.gaussian_filter(chm.reshape(hh, w), sigma=1.0)

    # Tree tops: local maxima in a 5 m window, at least MIN_HEIGHT tall, with enough points around.
    peaks = (chm == ndimage.maximum_filter(chm, size=5)) & (chm >= MIN_HEIGHT)
    support = ndimage.uniform_filter(count.astype(np.float32), size=3) * 9
    peaks &= support >= MIN_SUPPORT
    pi, pj = np.nonzero(peaks)
    crown = crown_cells(raw, pi, pj, chm[pi, pj])
    big = crown >= np.where(chm[pi, pj] > TALL_M, MIN_CROWN_CELLS_TALL, MIN_CROWN_CELLS)
    pi, pj, crown = pi[big], pj[big], crown[big]
    px, py = gx0 + (pj + 0.5) * CELL, gy0 + (pi + 0.5) * CELL
    inside = (px >= x0) & (px < x1) & (py >= y0) & (py < y1)
    px, py, ph, crown = px[inside], py[inside], chm[pi[inside], pj[inside]], crown[inside]
    radius = np.clip(0.18 * ph + 1.2, 1.5, 9.0)
    # No trunks in the middle of a road or on a railway: those tops are the L's steel,
    # signals and vehicles (33 "trees" stood in Lake Street under the L). Roadside trees
    # whose crowns overhang the street are outside the inner 80% of the carriageway.
    blocked = _carriageways_and_rail(feats, grown)
    if blocked is not None and len(px):
        free = ~shapely.contains_xy(blocked, px, py)
        px, py, ph, radius, crown = px[free], py[free], ph[free], radius[free], crown[free]
    # Boats, waves and harbor structures are sometimes classed as vegetation.
    water = feats[((feats["natural"] == "water") | (feats["waterway"] == "riverbank"))
                  & feats.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry
    if len(water) and len(px):
        wet = shapely.union_all([shapely.clip_by_rect(shapely.make_valid(g), *grown) for g in water])
        dry = ~shapely.contains_xy(wet, px, py)
        px, py, ph, radius, crown = px[dry], py[dry], ph[dry], radius[dry], crown[dry]
    out = pd.DataFrame({"x": px, "y": py, "base": dem.sample(px, py), "height": ph, "radius": radius,
                        "crown_cells": crown, "survey": lidar_source()})
    if lidar_source() == "cook2022":
        out = add_confirmed_2017(key, out, x, y, h, dem)
    return out


def add_confirmed_2017(key: Key, trees: pd.DataFrame, x, y, h, dem) -> pd.DataFrame:
    """2022 trees plus the 2017 trees the 2022 survey still sees vegetation at.

    The 2022 flight was early in the leaf-out, so small and late trees have thin crowns
    and many aren't detected; its classes are much cleaner, though. A 2017 tree with no
    2022 tree nearby is kept when 2022 vegetation points (branches) stand at its spot, at
    least half its old height. Trees cut down since, and 2017 'trees' that were poles or
    facades, have none. On the Loop and Near North Side this keeps more trees with canopy
    in the 2025 photos than either survey alone, at 2022's precision (28% failing vs 38%).
    """
    from scipy.spatial import cKDTree
    old = _load_2017(key)
    if old is None or not len(old) or not len(x):
        return trees
    if len(trees):
        dist = cKDTree(trees[["x", "y"]].values).query(old[["x", "y"]].values, distance_upper_bound=6.0)[0]
        old = old[~np.isfinite(dist) | (dist > 0.8 * old["radius"].values)]
    pts = cKDTree(np.c_[x, y])
    rows = []
    for r in old.itertuples():
        idx = pts.query_ball_point([r.x, r.y], max(1.5, 0.5 * r.radius))
        if len(idx) >= 3 and h[idx].max() >= 0.5 * r.height:
            height = float(h[idx].max())
            rows.append({"x": r.x, "y": r.y, "base": float(dem.sample(np.array([r.x]), np.array([r.y]))[0]),
                         "height": height, "radius": float(np.clip(0.18 * height + 1.2, 1.5, 9.0)),
                         "crown_cells": int(r.crown_cells), "survey": "2017 confirmed by 2022"})
    return pd.concat([trees, pd.DataFrame(rows)], ignore_index=True) if rows else trees


def _load_2017(key: Key) -> pd.DataFrame | None:
    """This block's 2017 trees, if they were made with the current settings."""
    from .config import WORK
    folder = WORK / "trees"
    path, stamp = folder / f"{key.i}_{key.j}.parquet", folder / f"{key.i}_{key.j}.settings"
    if not path.exists() or not stamp.exists() or stamp.read_text() != settings().replace(" +2017", ""):
        return None
    return pd.read_parquet(path)


def _carriageways_and_rail(feats, bounds):
    """Inner 80% of mapped road carriageways, railway tracks (1.5 m each side) and elevated rail (8 m)."""
    from .ground import ROAD_WIDTH, _is_lower_level, _width
    lines = feats.geometry.geom_type.isin(("LineString", "MultiLineString"))
    roads = feats[lines & feats["highway"].isin(ROAD_WIDTH.keys())]
    rail = feats[lines & feats["railway"].isin(["rail", "subway", "light_rail", "tram", "narrow_gauge"])]
    parts = []
    for _, r in roads.iterrows():
        if not _is_lower_level(r):  # tunnels and lower decks don't stop trees above them
            parts.append(r.geometry.buffer(0.4 * _width(r, ROAD_WIDTH), cap_style="flat"))
    for _, r in rail.iterrows():
        if _is_lower_level(r):
            continue
        # An elevated structure (the L) spans the whole street, with girders at the curbs.
        elevated = r.get("bridge") not in (None, "no") or str(r.get("layer") or "0").lstrip("-").isdigit() and int(r.get("layer") or 0) > 0
        parts.append(r.geometry.buffer(8.0 if elevated else 1.5, cap_style="flat"))
    if not parts:
        return None
    out = shapely.clip_by_rect(shapely.union_all(parts), *bounds)
    shapely.prepare(out)
    return out


def crown_cells(raw: np.ndarray, pi, pj, peak_h, reach: int = 3) -> np.ndarray:
    """For each peak, the 1 m cells within `reach` cells whose height is at least half the peak's."""
    hh, w = raw.shape
    out = np.zeros(len(pi), np.int32)
    for k, (a, b, h) in enumerate(zip(pi, pj, peak_h)):
        win = raw[max(a - reach, 0):min(a + reach + 1, hh), max(b - reach, 0):min(b + reach + 1, w)]
        out[k] = int((win >= 0.5 * h).sum())
    return out


def settings() -> str:
    """The detection settings a cached block was made with; blocks with others are redone."""
    base = f"v3 min_height={MIN_HEIGHT} crown={MIN_CROWN_CELLS}/{MIN_CROWN_CELLS_TALL}@{TALL_M}m roads+rail8"
    return base + " +2017" if lidar_source() == "cook2022" else base


def _stamp(key: Key):
    return TREES / f"{key.i}_{key.j}.settings"


def _job(key: Key):
    t0 = time.time()
    df = detect(key)
    df.to_parquet(TREES / f"{key.i}_{key.j}.parquet")
    _stamp(key).write_text(settings())
    return key, len(df), time.time() - t0


def run(blocks, force: bool = False) -> None:
    TREES.mkdir(parents=True, exist_ok=True)
    def current(k):
        return (TREES / f"{k.i}_{k.j}.parquet").exists() and _stamp(k).exists() and _stamp(k).read_text() == settings()
    todo = [k for k in sorted(blocks) if (force or not current(k)) and block_paths(k)[0].exists()]
    if lidar_source() == "cook2022":
        missing = sum(1 for k in todo if _load_2017(k) is None)
        if missing:
            print(f"  trees: {missing} blocks have no current 2017 trees to merge; run the trees step "
                  "with lidar.source ept2017 first")
    if not todo:
        print("  trees: all blocks up to date")
        return
    workers = lidar_workers(load_config()["lidar"]["workers"])
    print(f"  trees: detecting in {len(todo)} blocks, {workers} workers")
    t0, total = time.time(), 0
    with ProcessPoolExecutor(mp_context=_FRESH, max_workers=workers) as pool:
        for n, (key, count, secs) in enumerate(pool.map(_job, todo), 1):
            total += count
            if n % 20 == 0 or n == len(todo):
                print(f"    {n}/{len(todo)} blocks, {total:,} trees, {time.time() - t0:.0f}s")


def load(key: Key) -> pd.DataFrame | None:
    p = TREES / f"{key.i}_{key.j}.parquet"
    return pd.read_parquet(p) if p.exists() else None


# ---------------------------------------------------------------- models

def _crown(rings: int, segments: int, center: float, half_height: float) -> tuple[np.ndarray, np.ndarray]:
    """A low-poly ellipsoid crown of unit radius around (0, 0, center), in local x/y/z-up."""
    verts = [[0, 0, center - half_height]]
    for r in range(1, rings):
        t = np.pi * r / rings
        for s in range(segments):
            a = 2 * np.pi * (s + 0.5 * (r % 2)) / segments
            verts.append([np.sin(t) * np.cos(a), np.sin(t) * np.sin(a), center - np.cos(t) * half_height])
    verts.append([0, 0, center + half_height])
    v = np.asarray(verts, float)
    tris = []
    for s in range(segments):
        tris.append([0, 1 + (s + 1) % segments, 1 + s])
    for r in range(rings - 2):
        a0, b0 = 1 + r * segments, 1 + (r + 1) * segments
        for s in range(segments):
            s1 = (s + 1) % segments
            tris += [[a0 + s, a0 + s1, b0 + s], [a0 + s1, b0 + s1, b0 + s]]
    top = len(v) - 1
    last = 1 + (rings - 2) * segments
    for s in range(segments):
        tris.append([last + s, last + (s + 1) % segments, top])
    return v, np.asarray(tris, np.uint32).reshape(-1)


def tree_model(detail: int):
    """(crown, trunk) MeshBuilders for a unit tree: 1 m tall, crown radius 1 m.

    Instances scale it by (radius, radius, height). detail 0 is the street-level model.
    """
    crown, trunk = mesh.MeshBuilder(), mesh.MeshBuilder()
    # 16 triangles for every crown: there are three million of them.
    rings, segs = 3, 4
    v, t = _crown(rings, segs, 0.62, 0.38)
    n = v - np.array([0, 0, 0.62])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
    crown.add(v, n, t)
    if detail == 0:
        mesh.add_extrusion(trunk, shapely.Point(0, 0).buffer(0.07, quad_segs=1), 0.0, 0.4, 0)
    return crown, trunk


# Which trees each tile level draws: minimum height in meters (None = no trees).
LEVEL_TREES = {0: 0.0, 1: 6.0, 2: 10.0}
