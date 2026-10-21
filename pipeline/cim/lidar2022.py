"""Cook County 2022 lidar: county LAS tiles, thinned and cached in local meters.

The county publishes one LAS file per 2,500 ft tile (about 1.8 GB, ~100 points/m²
counting every return, state plane feet). On first use a tile is downloaded,
converted to local meters (z = NAVD88 m - ground_datum, as for 2017), thinned to
about `lidar.cook2022_density` points/m², stripped of noise and wires, and saved
under data/raw/lidar_cook_2022/ as LAZ. The original is then deleted.

`read_points` has the same contract as lidar.read_points, so everything
downstream (ground model, roof statistics, roofer, trees, rail) works unchanged.
"""
import fcntl
import json
import os
import time
from functools import lru_cache

import laspy
import numpy as np
import requests
import shapely
from pyproj import Transformer

from .config import RAW, load_config, load_sources
from .frame import local_frame

RAW_2022 = RAW / "lidar_cook_2022"
INDEX = RAW_2022 / "tiles.geojson"
STATE_PLANE = "EPSG:6455"  # NAD83(2011) / Illinois East (ftUS); the files carry no CRS record
FT = 0.3048006096012192

# ASPRS LAS 1.4 codes, as listed in the county metadata. Low noise and wires are dropped;
# road surface counts as ground, which is what the ground model and roofs expect.
# "High noise" (18) and "transmission tower" (15) are mostly tower roofs downtown: in the
# Loop tile, 79% of class-18 points fall inside building footprints at the building's
# height (it's 12% of all points). So they're kept as unclassified, and only points
# above Z_MAX (Willis Tower's roof is 442 m) or far below the street are dropped.
DROP = {7, 13, 14, 16, 20}  # low noise, wire guard/conductor/connector, ignore ground
REMAP = {11: 2, 15: 1, 18: 1}  # road surface -> ground; tower, high noise -> unclassified
Z_MAX, Z_MIN = 460.0, -30.0  # local meters


def _cfg() -> dict:
    return load_config()["lidar"]


@lru_cache
def _to_local() -> Transformer:
    return Transformer.from_crs(STATE_PLANE, local_frame().crs, always_xy=True)


# ---------------------------------------------------------------- tile index

def fetch_index() -> dict:
    """County tiles that touch the city's bounding box, with local-frame bounds."""
    src = load_sources()["lidar_cook_2022"]
    x0, y0, x1, y1 = load_config()["city_bbox"]
    feats, offset = [], 0
    while True:
        r = requests.get(src["index"] + "/query", timeout=120, params={
            "where": "1=1", "geometry": f"{x0},{y0},{x1},{y1}", "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": "TILE,OBJECTID",
            "outSR": 6455, "returnGeometry": "true", "resultOffset": offset, "f": "geojson"})
        r.raise_for_status()
        page = r.json()["features"]
        feats += page
        if len(page) < 2000:
            break
        offset += len(page)
    t = _to_local()
    out = {}
    for f in feats:
        ring = np.asarray(f["geometry"]["coordinates"][0])
        lx, ly = t.transform(ring[:, 0], ring[:, 1])
        out[f["properties"]["TILE"]] = [float(lx.min()), float(ly.min()), float(lx.max()), float(ly.max())]
    RAW_2022.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(out))
    return out


@lru_cache
def index() -> dict:
    return json.loads(INDEX.read_text()) if INDEX.exists() else fetch_index()


def tiles_for(bounds) -> list[str]:
    x0, y0, x1, y1 = bounds
    return sorted(t for t, (a, b, c, d) in index().items() if a < x1 and c > x0 and b < y1 and d > y0)


# ---------------------------------------------------------------- download and convert

def tile_path(tile: str):
    return RAW_2022 / f"{tile}.laz"


def _download(tile: str, dest) -> None:
    url = load_sources()["lidar_cook_2022"]["url"].format(tile=tile)
    part = dest.with_suffix(".part")
    for attempt in range(6):
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(8 << 20):
                        f.write(chunk)
            part.rename(dest)
            return
        except requests.RequestException:
            if attempt == 5:
                raise
            time.sleep(10 * 2 ** attempt)


def convert(src, dest, density: float, seed: int = 0) -> int:
    """Thin a county LAS file to `density` points/m² in local meters; returns points kept."""
    rng = np.random.default_rng(seed)
    datum = float(load_config()["ground_datum"])
    with laspy.open(src) as f:
        h = f.header
        area_m2 = (h.maxs[0] - h.mins[0]) * (h.maxs[1] - h.mins[1]) * FT * FT
        keep = min(1.0, density * area_m2 / max(h.point_count, 1))
        parts = []
        for pts in f.chunk_iterator(5_000_000):
            c = np.asarray(pts.classification)
            m = ~np.isin(c, list(DROP)) & (rng.random(len(c)) < keep)
            if not m.any():
                continue
            z_all = np.asarray(pts.z) * FT - datum
            m &= (z_all < Z_MAX) & (z_all > Z_MIN)
            if not m.any():
                continue
            x, y = _to_local().transform(np.asarray(pts.x)[m], np.asarray(pts.y)[m])
            z = z_all[m]
            cm = c[m].copy()
            for a, b in REMAP.items():
                cm[cm == a] = b
            parts.append((x, y, z, cm))
    x, y, z, c = (np.concatenate(p) for p in zip(*parts)) if parts else (np.empty(0),) * 3 + (np.empty(0, np.uint8),)
    header = laspy.LasHeader(point_format=1, version="1.4")
    header.scales = np.array([0.01, 0.01, 0.01])
    header.offsets = np.array([np.floor(x.min()) if len(x) else 0.0, np.floor(y.min()) if len(y) else 0.0, 0.0])
    las = laspy.LasData(header)
    las.x, las.y, las.z = x, y, z
    las.classification = c.astype(np.uint8)
    tmp = dest.with_suffix(".tmp.laz")
    las.write(tmp)
    tmp.rename(dest)
    return len(x)


def ensure(tile: str) -> None:
    """Download and convert one tile unless it's cached. Safe to call from many processes."""
    dest = tile_path(tile)
    if dest.exists():
        return
    RAW_2022.mkdir(parents=True, exist_ok=True)
    with open(RAW_2022 / f"{tile}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if dest.exists():
            return
        raw = RAW_2022 / "las" / f"{tile}.las"
        raw.parent.mkdir(exist_ok=True)
        if not raw.exists():
            _download(tile, raw)
        convert(raw, dest, _cfg()["cook2022_density"], seed=int(tile) % (2 ** 32))
        if not _cfg().get("cook2022_keep_las"):
            os.remove(raw)


# ---------------------------------------------------------------- reading

def read_points(bounds_local, depth: int | None = None):
    """Points inside a local-frame box: (x, y, z_local, classification). `depth` is ignored."""
    x0, y0, x1, y1 = bounds_local
    parts = []
    for tile in tiles_for(bounds_local):
        ensure(tile)
        las = laspy.read(tile_path(tile))
        x, y = np.asarray(las.x), np.asarray(las.y)
        m = (x >= x0) & (x < x1) & (y >= y0) & (y < y1)
        if m.any():
            parts.append((x[m], y[m], np.asarray(las.z)[m], np.asarray(las.classification)[m]))
    if not parts:
        e = np.empty(0)
        return e, e, e, np.empty(0, np.uint8)
    return tuple(np.concatenate(p) for p in zip(*parts))


def tiles_in(polygon) -> list[str]:
    """County tiles overlapping a local-frame polygon (for fetching ahead of a build)."""
    return sorted(t for t, b in index().items() if shapely.box(*b).intersects(polygon))


def _fetch_job(tile: str):
    t0 = time.time()
    ensure(tile)
    return tile, time.time() - t0


def fetch_for(blocks, jobs: int | None = None, margin: float = 100.0) -> None:
    """Download and convert every county tile the given 2 km blocks need, `jobs` at a time."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    need = set()
    for key in blocks:
        x0, y0, x1, y1 = key.bounds
        need.update(tiles_for((x0 - margin, y0 - margin, x1 + margin, y1 + margin)))
    todo = sorted(t for t in need if not tile_path(t).exists())
    if not todo:
        print(f"  lidar 2022: all {len(need)} tiles cached")
        return
    jobs = jobs or _cfg().get("cook2022_downloads", 4)
    print(f"  lidar 2022: fetching {len(todo)} of {len(need)} tiles, {jobs} at a time", flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(mp_context=multiprocessing.get_context("forkserver"), max_workers=jobs) as pool:
        for n, (tile, secs) in enumerate(pool.map(_fetch_job, todo), 1):
            if n % 5 == 0 or n == len(todo):
                left = (time.time() - t0) / n * (len(todo) - n)
                print(f"    {n}/{len(todo)} tiles, {time.time() - t0:.0f}s (last {secs:.0f}s), ~{left / 60:.0f} min left",
                      flush=True)
