"""Roof shapes (LoD 2.2) with roofer, from the same lidar points, per 2 km block.

roofer (https://github.com/3DBAG/roofer, GPL-3.0, run as a separate program)
detects roof planes in each footprint's points and builds a watertight model.
Its input is our own point cloud: ground points as class 2, and points above the
ground inside a footprint as class 6. The survey's own building class misses
many downtown roofs (they're classed as vegetation), so we don't rely on it.

Per block this writes data/work/roofs/<i>_<j>.npz: triangles for every building
roofer reconstructed, in local meters, plus the building ids and offsets.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import geopandas as gpd
import laspy
import numpy as np
import shapely

from .config import WORK, load_config
from .frame import local_frame
from .grid import BLOCK_LEVEL, Key
from .lidar import BRIDGE, GROUND, HIGH_NOISE, NOISE, WATER, Dem, block_paths, fingerprint, is_current, read_points

# Workers start from a fresh interpreter instead of forking: a fork shares the parent's
# memory copy-on-write, and Python's reference counting soon copies all of it (the
# parent holds every building in the city), multiplying memory by the worker count.
_FRESH = multiprocessing.get_context("forkserver")

ROOFS = WORK / "roofs"
MARGIN = 10.0


def roof_path(key: Key) -> Path:
    return ROOFS / f"{key.i}_{key.j}.npz"


def _stamp(key: Key) -> Path:
    return ROOFS / f"{key.i}_{key.j}.buildings.sha256"


def _write_inputs(tmp: Path, key: Key, buildings: gpd.GeoDataFrame, depth: int) -> bool:
    x0, y0, x1, y1 = key.bounds
    fx0, fy0, fx1, fy1 = buildings.total_bounds
    grown = (min(x0, fx0) - MARGIN, min(y0, fy0) - MARGIN, max(x1, fx1) + MARGIN, max(y1, fy1) + MARGIN)
    x, y, z, c = read_points(grown, depth)
    if not len(x):
        return False
    dem = Dem.load(block_paths(key)[0])
    ground = z < dem.sample(x, y) + 1.5
    keep_ground = (c == GROUND) & ground
    candidate = ~ground & (c != NOISE) & (c != HIGH_NOISE) & (c != WATER) & (c != BRIDGE)
    idx = np.flatnonzero(candidate)
    tree = shapely.STRtree(shapely.buffer(buildings.geometry.values, 0.5))
    inside = np.unique(tree.query(shapely.points(x[idx], y[idx]), predicate="within")[0])
    building = np.zeros(len(x), bool)
    building[idx[inside]] = True
    sel = keep_ground | building
    if not building.any():
        return False

    header = laspy.LasHeader(point_format=1, version="1.4")
    header.scales = np.array([0.01, 0.01, 0.01])
    header.offsets = np.array([x0, y0, 0.0])
    las = laspy.LasData(header)
    las.x, las.y, las.z = x[sel], y[sel], z[sel]
    las.classification = np.where(building[sel], 6, 2).astype(np.uint8)
    las.write(tmp / "points.laz")

    return True


def _triangulate_surface(rings: list[np.ndarray]):
    """Triangles for one planar 3D polygon (outer ring + holes)."""
    outer = rings[0]
    # Newell's method gives the polygon normal, even for non-convex rings.
    p, q = outer, np.roll(outer, -1, axis=0)
    n = np.array([np.sum((p[:, 1] - q[:, 1]) * (p[:, 2] + q[:, 2])),
                  np.sum((p[:, 2] - q[:, 2]) * (p[:, 0] + q[:, 0])),
                  np.sum((p[:, 0] - q[:, 0]) * (p[:, 1] + q[:, 1]))])
    length = np.linalg.norm(n)
    if length < 1e-9:
        return None
    n /= length
    drop = int(np.argmax(np.abs(n)))
    keep = [a for a in range(3) if a != drop]
    verts = np.concatenate(rings)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    import mapbox_earcut as earcut
    tris = np.asarray(earcut.triangulate_float64(verts[:, keep], ends), np.uint32)
    if not len(tris):
        return None
    return verts, tris, n


def _read_cityjson(outdir: Path) -> dict[str, tuple]:
    """Building id -> (positions, normals, indices) from roofer's CityJSONSeq output."""
    out = {}
    for path in sorted(outdir.rglob("*.city.jsonl")):
        lines = path.read_text().splitlines()
        if not lines:
            continue
        tr = json.loads(lines[0]).get("transform", {"scale": [1, 1, 1], "translate": [0, 0, 0]})
        scale, translate = np.asarray(tr["scale"]), np.asarray(tr["translate"])
        for line in lines[1:]:
            feat = json.loads(line)
            verts = np.asarray(feat["vertices"], np.float64) * scale + translate
            for oid, obj in feat["CityObjects"].items():
                geoms = [g for g in obj.get("geometry", []) if str(g.get("lod")) == "2.2"]
                if not geoms:
                    continue
                parent = (obj.get("parents") or [oid])[0]
                bid = feat["CityObjects"].get(parent, {}).get("attributes", {}).get("id") or parent
                pos, nrm, idx = out.get(bid, ([], [], []))
                count = sum(len(p) for p in pos)
                for g in geoms:
                    shells = g["boundaries"] if g["type"] == "Solid" else [g["boundaries"]]
                    for shell in shells:
                        for surface in shell:
                            tri = _triangulate_surface([verts[np.asarray(r)] for r in surface])
                            if tri is None:
                                continue
                            v, t, n = tri
                            pos.append(v)
                            nrm.append(np.tile(n, (len(v), 1)))
                            idx.append(t + count)
                            count += len(v)
                out[bid] = (pos, nrm, idx)
    return {k: (np.concatenate(p), np.concatenate(n), np.concatenate(i)) for k, (p, n, i) in out.items() if p}


def _timeout(n: int) -> float:
    """Seconds to allow roofer for `n` footprints, well above its normal pace (a few ms each)."""
    return 120 + 0.3 * n


def _roofer(tmp: Path, footprints: gpd.GeoDataFrame, jobs: int, name: str) -> tuple[dict, list[str]]:
    """Run roofer on `footprints`; if it fails, split them in half and retry each half.

    roofer occasionally crashes on a particular footprint, or spins on one forever
    (one core busy, memory flat), so a run that takes too long counts as a failure.
    Bisecting isolates those, so the rest of the block still gets roofs. Returns
    (models, ids roofer couldn't do).
    """
    run_dir = tmp / name
    run_dir.mkdir()
    fp = footprints[["id", "base", "height", "geometry"]].copy()
    fp["roof"] = fp["base"] + fp["height"]  # fallback for buildings roofer can't see
    fp.to_file(run_dir / "footprints.gpkg", driver="GPKG")
    cmd = ["roofer", "-j", str(jobs), "--id-attribute", "id", "--h-roof-attribute", "roof",
           "--lod22", str(tmp / "points.laz"), str(run_dir / "footprints.gpkg"), str(run_dir / "out")]
    env = {**os.environ, "GDAL_DATA": "/opt/roofer/share/gdal", "PROJ_DATA": "/opt/roofer/share/proj"}
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=run_dir,  # it writes a log to cwd
                           timeout=_timeout(len(footprints)))
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:  # run() kills it
        ok = False
    if ok:
        models = _read_cityjson(run_dir / "out")
        shutil.rmtree(run_dir)
        return models, []
    shutil.rmtree(run_dir)
    if len(footprints) == 1:
        return {}, [footprints["id"].iloc[0]]
    half = len(footprints) // 2
    m1, f1 = _roofer(tmp, footprints.iloc[:half], jobs, name + "a")
    m2, f2 = _roofer(tmp, footprints.iloc[half:], jobs, name + "b")
    return m1 | m2, f1 + f2


def _block_job(args):
    key, bpath, depth, jobs, fp = args
    t0 = time.time()
    buildings = gpd.read_parquet(bpath)
    failed: list[str] = []
    try:
        with tempfile.TemporaryDirectory(dir=WORK) as td:
            tmp = Path(td)
            models = {}
            if _write_inputs(tmp, key, buildings, depth):
                # One roofer run per 1 km quadrant: its memory grows with the area it
                # covers (about 10 GB for a full 2 km block), the point cloud is shared.
                x0, y0, x1, y1 = key.bounds
                qx = (buildings["cx"] >= (x0 + x1) / 2).astype(int)
                qy = (buildings["cy"] >= (y0 + y1) / 2).astype(int)
                for q in range(4):
                    sel = buildings[(qx * 2 + qy) == q]
                    if len(sel):
                        m, f = _roofer(tmp, sel, jobs, f"q{q}")
                        models |= m
                        failed += f
    except Exception as e:  # never stop the whole city for one block; it keeps extruded buildings
        print(f"    block {key.name}: {type(e).__name__}: {e}", flush=True)
        models, failed = {}, list(buildings["id"])
    ids = np.array(list(models), str)
    offsets_v = np.cumsum([0] + [len(models[i][0]) for i in ids])
    offsets_t = np.cumsum([0] + [len(models[i][2]) for i in ids])
    np.savez_compressed(
        roof_path(key), ids=ids, offsets_v=offsets_v, offsets_t=offsets_t, failed=np.array(failed, str),
        positions=np.concatenate([models[i][0] for i in ids]).astype(np.float32) if len(ids) else np.zeros((0, 3)),
        normals=np.concatenate([models[i][1] for i in ids]).astype(np.float32) if len(ids) else np.zeros((0, 3)),
        indices=np.concatenate([models[i][2] for i in ids]).astype(np.uint32) if len(ids) else np.zeros(0),
    )
    _stamp(key).write_text(fp)
    return key, len(buildings), len(ids), len(failed), time.time() - t0


def run(blocks, buildings: gpd.GeoDataFrame, force: bool = False) -> None:
    """Reconstruct roofs for buildings that have lidar heights and aren't building parts or podiums."""
    ROOFS.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    depth = cfg["lidar"]["depth"]
    # Podiums around towers mapped as parts stay flat: their points include tower walls.
    podium = buildings["has_parts"].fillna(False).astype(bool) if "has_parts" in buildings else False
    cand = buildings[(buildings["height_source"] == "lidar") & (buildings["kind"] != "part") & ~podium]
    from .grid import index as grid_index
    bi, bj = grid_index(cand["cx"].values, cand["cy"].values, BLOCK_LEVEL)
    tmp = WORK / "roofs-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    jobs = []
    workers = cfg["roofs"]["workers"]
    threads = cfg["roofs"]["threads_per_block"]
    for key in sorted(blocks):
        sel = cand[(bi == key.i) & (bj == key.j)]
        if not len(sel):
            continue
        fp = fingerprint(sel)
        if not force and is_current(_stamp(key), [roof_path(key)], fp):
            continue
        path = tmp / f"{key.i}_{key.j}.parquet"
        sel[["id", "base", "height", "cx", "cy", "geometry"]].to_parquet(path)
        jobs.append((key, path, depth, threads, fp))
    if not jobs:
        print("  roofs: all blocks up to date")
        return
    print(f"  roofs: {len(jobs)} blocks, {workers} at a time, {threads} roofer jobs each")
    t0 = time.time()
    done = made = crashed = 0
    with ProcessPoolExecutor(mp_context=_FRESH, max_workers=workers) as pool:
        for n, (key, nb, nm, nf, secs) in enumerate(pool.map(_block_job, jobs), 1):
            done += nb
            made += nm
            crashed += nf
            if n % 5 == 0 or n == len(jobs):
                print(f"    {n}/{len(jobs)} blocks, {made:,} of {done:,} buildings reconstructed "
                      f"({crashed} skipped after roofer crashes or hangs), {time.time() - t0:.0f}s (last block {secs:.0f}s)",
                      flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


def load(key: Key) -> dict[str, tuple] | None:
    """Building id -> (positions, normals, indices) for one block, or None if not built."""
    p = roof_path(key)
    if not p.exists():
        return None
    d = np.load(p)
    ids = d["ids"]
    if not len(ids):
        return {}
    # Read each array once: indexing an NpzFile decompresses the whole array every time.
    ov, ot = d["offsets_v"], d["offsets_t"]
    pos, nrm, idx = d["positions"], d["normals"], d["indices"]
    return {str(bid): (pos[ov[k]:ov[k + 1]], nrm[ov[k]:ov[k + 1]], idx[ot[k]:ot[k + 1]])
            for k, bid in enumerate(ids)}
