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
from .config import WORK, load_config
from .grid import Key
from .lidar import block_paths, load_dem, read_points
from .osm import load_features

_FRESH = multiprocessing.get_context("forkserver")

TREES = WORK / "trees"
VEGETATION = (3, 4, 5)
CELL = 1.0
MIN_HEIGHT = 3.0


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
        return pd.DataFrame(columns=["x", "y", "base", "height", "radius"])

    gx0, gy0 = grown[0], grown[1]
    w, hh = int(np.ceil((grown[2] - gx0) / CELL)), int(np.ceil((grown[3] - gy0) / CELL))
    j = np.clip(((x - gx0) / CELL).astype(int), 0, w - 1)
    i = np.clip(((y - gy0) / CELL).astype(int), 0, hh - 1)
    chm = np.zeros(w * hh, np.float32)
    np.maximum.at(chm, i * w + j, h.astype(np.float32))
    count = np.bincount(i * w + j, minlength=w * hh).reshape(hh, w)
    chm = ndimage.gaussian_filter(chm.reshape(hh, w), sigma=1.0)

    # Tree tops: local maxima in a 5 m window, at least MIN_HEIGHT tall, with enough points around.
    peaks = (chm == ndimage.maximum_filter(chm, size=5)) & (chm >= MIN_HEIGHT)
    support = ndimage.uniform_filter(count.astype(np.float32), size=3) * 9
    peaks &= support >= 6
    pi, pj = np.nonzero(peaks)
    px, py = gx0 + (pj + 0.5) * CELL, gy0 + (pi + 0.5) * CELL
    inside = (px >= x0) & (px < x1) & (py >= y0) & (py < y1)
    px, py, ph = px[inside], py[inside], chm[pi[inside], pj[inside]]
    radius = np.clip(0.18 * ph + 1.2, 1.5, 9.0)
    # Boats, waves and harbor structures are sometimes classed as vegetation.
    water = feats[((feats["natural"] == "water") | (feats["waterway"] == "riverbank"))
                  & feats.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry
    if len(water) and len(px):
        wet = shapely.union_all([shapely.clip_by_rect(shapely.make_valid(g), *grown) for g in water])
        dry = ~shapely.contains_xy(wet, px, py)
        px, py, ph, radius = px[dry], py[dry], ph[dry], radius[dry]
    return pd.DataFrame({"x": px, "y": py, "base": dem.sample(px, py), "height": ph, "radius": radius})


def _job(key: Key):
    t0 = time.time()
    df = detect(key)
    df.to_parquet(TREES / f"{key.i}_{key.j}.parquet")
    return key, len(df), time.time() - t0


def run(blocks, force: bool = False) -> None:
    TREES.mkdir(parents=True, exist_ok=True)
    todo = [k for k in sorted(blocks)
            if (force or not (TREES / f"{k.i}_{k.j}.parquet").exists()) and block_paths(k)[0].exists()]
    if not todo:
        print("  trees: all blocks up to date")
        return
    workers = load_config()["lidar"]["workers"]
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
