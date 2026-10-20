"""Package buildings and ground into a 3D Tiles quadtree.

Level 0 tiles (500 m) hold every building and every ground layer. Each level up
doubles the tile size and keeps less: fewer, simpler buildings and fewer ground
layers. The viewer swaps a tile for its four children as the camera gets close
(refine REPLACE), so distant parts of the city cost little.

Work is split into 2 km blocks (level 2) that run in parallel. Levels above that
are built from the blocks' summaries.
"""
import json
import math
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

from . import mesh
from .areas import area_polygon_local, area_row
from .config import BUILDS, LOCK_FILE, WORK, area_dir, load_config, load_sources
from .frame import local_frame
from .gltf import write_glb
from .grid import BLOCK_LEVEL, Key, leaves_covering
from .grid import index as grid_index
from .ground import LEVEL_LAYERS, UPPER_LAYERS, raw_layers, stack
from .osm import load_features

# Default colors so the tiles look reasonable in any 3D Tiles viewer.
# The app replaces these with its own materials by name.
MATERIALS = {
    "building": (0.86, 0.84, 0.80, 1.0),
    "land": (0.80, 0.79, 0.76, 1.0),
    "green": (0.62, 0.72, 0.52, 1.0),
    "sand": (0.89, 0.84, 0.70, 1.0),
    "water": (0.42, 0.62, 0.74, 1.0),
    "road": (0.55, 0.55, 0.56, 1.0),
    "path": (0.90, 0.89, 0.86, 1.0),
}
MAT_INDEX = {name: i for i, name in enumerate(MATERIALS)}
GROUND_ORDER = ["path", "road", "water", "sand", "green", "land"]
GROUND_CELL = 125.0  # earcut is unreliable on whole-tile polygons with many holes
GEOMETRIC_ERROR_L1 = 10.0  # meters; doubles each level up, leaves are 0

# How each level draws buildings. Buildings at least `height` m tall or with a footprint
# of at least `area` m² stay individual (footprints simplified by `tol` m). The rest are
# merged into block-sized masses: footprints grown by `merge` m so neighbors touch,
# unioned, shrunk back, and extruded to their members' average height. Masses smaller
# than `min_mass` m² are dropped. merge=None drops the rest instead. Levels above the
# table use TOP.
LEVEL_BUILDINGS = {
    0: dict(height=0, area=0, tol=0.1),
    1: dict(height=20, area=2000, tol=0.5, merge=1.5, min_mass=60),
    2: dict(height=30, area=5000, tol=1.5, merge=3.0, min_mass=600),
    3: dict(height=45, area=20000, tol=4.0, merge=6.0, min_mass=6000),
    4: dict(height=70, area=math.inf, tol=6.0),
}
TOP_BUILDINGS = dict(height=110, area=math.inf, tol=8.0)


def _ground_simplify(level: int) -> float:
    return 0.0 if level == 0 else float(2 ** (level - 1))


def _workers() -> int:
    return max(1, min(os.cpu_count() or 1, int(os.environ.get("CIM_WORKERS", 16))))


# ---------------------------------------------------------------- mesh content

def _polys(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in _polys(g)]
    return []


def _cells(bounds, size: float):
    x0, y0, x1, y1 = bounds
    n = max(1, round((x1 - x0) / size))
    step = (x1 - x0) / n
    return [(x0 + i * step, y0 + j * step, x0 + (i + 1) * step, y0 + (j + 1) * step)
            for i in range(n) for j in range(n)]


def _clip_cell(geom, bounds):
    """Clip to a triangulation cell and snap to 1 cm. clip_by_rect is fast but can
    return invalid geometry, so fall back to a full intersection when needed."""
    try:
        return shapely.set_precision(shapely.make_valid(shapely.clip_by_rect(geom, *bounds)), 0.01)
    except shapely.errors.GEOSException:
        sub = shapely.make_valid(geom).intersection(box(*bounds))
        try:
            return shapely.set_precision(sub, 0.01)
        except shapely.errors.GEOSException:
            return sub


def _feature(b) -> dict:
    f = {"id": b["id"], "h": round(float(b["height"]), 1), "src": b["height_source"]}
    if b["min_height"]:
        f["minH"] = round(float(b["min_height"]), 1)
    if isinstance(b["name"], str) and b["name"]:
        f["name"] = b["name"]
    if pd.notna(b["year_built"]):
        f["year"] = int(b["year_built"])
    if isinstance(b["osm_id"], str):
        f["osm"] = b["osm_id"]
    return f


def _rules(level: int) -> dict:
    return LEVEL_BUILDINGS.get(level, TOP_BUILDINGS)


def _keep(buildings: gpd.GeoDataFrame, level: int) -> gpd.GeoDataFrame:
    """Buildings drawn individually at this level."""
    r = _rules(level)
    if level == 0 or not len(buildings):
        return buildings
    return buildings[(buildings["fp_area"] >= r["area"]) | (buildings["height"] >= r["height"])]


def _masses(buildings: gpd.GeoDataFrame, level: int) -> list[tuple]:
    """Merged (polygon, height) masses standing in for the smaller buildings at this level."""
    r = _rules(level)
    if level == 0 or not r.get("merge") or not len(buildings):
        return []
    small = buildings[(buildings["fp_area"] < r["area"]) & (buildings["height"] < r["height"])]
    small = small[small["min_height"] < 1.0]  # skip floating parts (canopies, skybridges)
    if not len(small):
        return []
    d = r["merge"]
    merged = shapely.union_all(shapely.buffer(small.geometry.values, d, quad_segs=1, join_style="mitre"))
    pieces = [p.buffer(-d, join_style="mitre") for p in _polys(merged)]
    tree = shapely.STRtree(shapely.points(small["cx"].values, small["cy"].values))
    heights, areas = small["height"].values, small["fp_area"].values
    out = []
    for piece in pieces:
        for poly in _polys(shapely.make_valid(piece.simplify(r["tol"]))):
            if poly.area < r["min_mass"]:
                continue
            members = tree.query(poly, predicate="contains")
            if not len(members):
                continue
            h = float(np.average(heights[members], weights=areas[members]))
            out.append((poly, h))
    return out


def _write_tile(out_dir, key: Key, ground: dict, buildings: gpd.GeoDataFrame) -> dict | None:
    """Write one tile's GLB. Returns its metadata, or None if it would be empty."""
    meshes = []
    cell = GROUND_CELL * 2 ** key.level
    for layer in GROUND_ORDER:
        geom = ground.get(layer)
        if geom is None or geom.is_empty:
            continue
        mb = mesh.MeshBuilder()
        for c in _cells(key.bounds, cell):
            sub = _clip_cell(geom, c)
            for poly in _polys(sub):
                mesh.add_flat(mb, poly, 0.0)
        if (arr := mb.arrays()) is not None:
            arr.pop("_FEATURE_ID_0")
            meshes.append({"name": layer, "material": MAT_INDEX[layer], "arrays": arr})

    max_h = 1.0
    masses = _masses(buildings, key.level) if len(buildings) else []
    buildings = _keep(buildings, key.level) if len(buildings) else buildings
    if masses:
        mb = mesh.MeshBuilder()
        for poly, h in masses:
            mesh.add_extrusion(mb, poly, 0.0, h, 0)
            max_h = max(max_h, h)
        if (arr := mb.arrays()) is not None:
            arr.pop("_FEATURE_ID_0")
            meshes.append({"name": "masses", "material": MAT_INDEX["building"], "arrays": arr})
    if len(buildings):
        tol = _rules(key.level)["tol"]
        mb = mesh.MeshBuilder()
        features = []
        for fid, (_, b) in enumerate(buildings.iterrows()):
            geom = b.geometry.simplify(tol)
            for poly in _polys(geom):
                mesh.add_extrusion(mb, poly, float(b["min_height"]), float(b["height"]), fid)
            features.append(_feature(b))
            max_h = max(max_h, float(b["height"]))
        if (arr := mb.arrays()) is not None:
            meshes.append({"name": "buildings", "material": MAT_INDEX["building"], "arrays": arr,
                           "extras": {"features": features}})
    if not meshes:
        return None
    uri = f"tiles/{key.level}/{key.i}_{key.j}.glb"
    (out_dir / uri).parent.mkdir(parents=True, exist_ok=True)
    nbytes = write_glb(out_dir / uri, meshes, [{"name": n, "color": c} for n, c in MATERIALS.items()])
    return {"uri": uri, "max_h": max_h, "bytes": nbytes,
            "triangles": sum(len(m["arrays"]["indices"]) // 3 for m in meshes),
            "buildings": len(buildings), "masses": len(masses)}


# ---------------------------------------------------------------- block worker

def _block_job(args):
    """Build levels 0–2 inside one 2 km block. Runs in a worker process."""
    block, leaves, out_dir, bpath = args
    t0 = time.time()
    leaf_extent = shapely.union_all([k.polygon() for k in leaves])
    margin = box(*block.bounds).buffer(40, join_style="mitre")
    feats = load_features(margin.bounds)
    feats = feats[feats.intersects(margin)]
    raw = raw_layers(feats, margin)
    buildings = gpd.read_parquet(bpath) if bpath else None

    metas, extents, summary = {}, {}, {}
    for level in range(0, BLOCK_LEVEL + 1):
        keys = sorted({k.ancestor(level) for k in leaves})
        for key in keys:
            ext = shapely.clip_by_rect(leaf_extent, *key.bounds)
            extents[key] = ext
            ground = stack(raw, key.polygon(), LEVEL_LAYERS[level], _ground_simplify(level))
            ground = {k: v.intersection(ext) for k, v in ground.items()} if level else ground
            tb = None
            if buildings is not None:
                inside = ((buildings["cx"] >= key.bounds[0]) & (buildings["cx"] < key.bounds[2])
                          & (buildings["cy"] >= key.bounds[1]) & (buildings["cy"] < key.bounds[3]))
                tb = buildings[inside]
            meta = _write_tile(out_dir, key, ground, tb if tb is not None else [])
            if meta:
                metas[key] = meta
            if level == BLOCK_LEVEL:
                summary[key] = {k: shapely.to_wkb(ground[k].simplify(2.0)) for k in UPPER_LAYERS if k in ground}
    # Major roads, water and parks for the minimap image.
    minimap = {k: shapely.to_wkb(v.intersection(leaf_extent))
               for k, v in stack(raw, box(*block.bounds), ["road_major", "water", "green"], 2.0).items()
               if k != "land"}
    return block, metas, {k: shapely.to_wkb(v) for k, v in extents.items()}, summary, minimap, time.time() - t0


# ---------------------------------------------------------------- build

def build(areas: list[int], build_name: str) -> dict:
    t_start = time.time()
    cfg = load_config()
    frame = local_frame()
    out_dir = BUILDS / build_name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    boundary = shapely.union_all([area_polygon_local(a) for a in areas])
    leaves = leaves_covering(boundary, cfg["ground_margin_tiles"])

    buildings = pd.concat([gpd.read_parquet(area_dir(a) / "buildings.parquet") for a in areas],
                          ignore_index=True).drop_duplicates("id")
    buildings = gpd.GeoDataFrame(buildings, geometry="geometry", crs=frame.crs)
    centers = buildings.geometry.representative_point()
    buildings["cx"], buildings["cy"] = centers.x.values, centers.y.values
    buildings["fp_area"] = buildings.geometry.area

    # Split buildings by block so each worker reads only its own.
    tmp = WORK / "tiles-tmp" / build_name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    bi, bj = grid_index(buildings["cx"].values, buildings["cy"].values, BLOCK_LEVEL)
    blocks: dict[Key, list[Key]] = {}
    for leaf in leaves:
        blocks.setdefault(leaf.ancestor(BLOCK_LEVEL), []).append(leaf)
    jobs = []
    for block, block_leaves in sorted(blocks.items()):
        sel = buildings[(bi == block.i) & (bj == block.j)]
        path = None
        if len(sel):
            path = tmp / f"{block.i}_{block.j}.parquet"
            sel.to_parquet(path)
        jobs.append((block, block_leaves, out_dir, path))

    metas, extents, summaries, minimap_parts = {}, {}, {}, []
    print(f"  {len(leaves)} leaf tiles in {len(jobs)} blocks, {_workers()} workers")
    with ProcessPoolExecutor(_workers()) as pool:
        for n, (block, m, ext, summ, mm, secs) in enumerate(pool.map(_block_job, jobs), 1):
            metas.update(m)
            extents.update({k: shapely.from_wkb(v) for k, v in ext.items()})
            summaries.update({k: {layer: shapely.from_wkb(g) for layer, g in s.items()} for k, s in summ.items()})
            minimap_parts.append(mm)
            if n % 10 == 0 or n == len(jobs):
                print(f"    {n}/{len(jobs)} blocks ({time.time() - t_start:.0f}s)")
    shutil.rmtree(tmp, ignore_errors=True)

    # Levels above the blocks, built from the blocks' simplified water and parks.
    level_keys = sorted(summaries)
    level = BLOCK_LEVEL
    while len(level_keys) > 1 or level_keys[0].level < BLOCK_LEVEL + 1:
        level += 1
        parents: dict[Key, list[Key]] = {}
        for k in level_keys:
            parents.setdefault(k.parent(), []).append(k)
        next_summaries = {}
        for key, kids in sorted(parents.items()):
            ext = shapely.union_all([extents[k] for k in kids])
            extents[key] = ext
            raw = {layer: shapely.union_all([summaries[k][layer] for k in kids if layer in summaries[k]])
                   for layer in UPPER_LAYERS}
            ground = stack(raw, key.polygon(), UPPER_LAYERS, _ground_simplify(level))
            ground = {k: v.intersection(ext) for k, v in ground.items()}
            x0, y0, x1, y1 = key.bounds
            inside = (buildings["cx"] >= x0) & (buildings["cx"] < x1) & (buildings["cy"] >= y0) & (buildings["cy"] < y1)
            meta = _write_tile(out_dir, key, ground, buildings[inside])
            if meta:
                metas[key] = meta
            next_summaries[key] = {k: v for k, v in ground.items() if k in UPPER_LAYERS}
        summaries = next_summaries
        level_keys = sorted(parents)
    root_key = level_keys[0]

    city_root = _node(root_key, metas, extents)
    city_root.pop("_top")
    far = _far_lake(out_dir, root_key, shapely.union_all(list(extents.values())))
    tileset = {
        "asset": {"version": "1.1", "generator": "chicago-interactive-map pipeline"},
        "geometricError": _geometric_error(root_key.level) * 2,
        # The far lake is always drawn (ADD); the city quadtree refines as usual.
        "root": {"boundingVolume": city_root["boundingVolume"], "geometricError": _geometric_error(root_key.level) * 2,
                 "refine": "ADD", "children": [city_root] + ([far] if far else []),
                 "transform": frame.enu_to_ecef().flatten(order="F").tolist()},
    }
    (out_dir / "tileset.json").write_text(json.dumps(tileset))
    (out_dir / "frame.json").write_text(json.dumps(frame.to_dict(), indent=2))

    from .minimap import render as render_minimap
    overview = render_minimap(out_dir, areas, buildings, minimap_parts, shapely.union_all(list(extents.values())))
    (out_dir / "overview.json").write_text(json.dumps(overview))

    by_level: dict[int, dict] = {}
    for key, m in metas.items():
        s = by_level.setdefault(key.level, {"tiles": 0, "bytes": 0, "triangles": 0})
        s["tiles"] += 1
        s["bytes"] += m["bytes"]
        s["triangles"] += m["triangles"]
    report = {
        "build": build_name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - t_start),
        "areas": [{"number": a, "name": area_row(a)["name"]} for a in areas],
        "buildings": int(len(buildings)),
        "height_sources": buildings["height_source"].value_counts().to_dict(),
        "building_kinds": buildings["kind"].value_counts().to_dict(),
        "tiles": len(metas),
        "bytes": sum(m["bytes"] for m in metas.values()),
        "levels": {str(k): v for k, v in sorted(by_level.items())},
        "tile_size_m": cfg["tile_size"],
        "sources": json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else {},
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    (out_dir / "attribution.json").write_text(json.dumps(_attribution(), indent=2))

    manifest = BUILDS / "manifest.json"
    manifest.write_text(json.dumps({"current": build_name, "builds": sorted(
        p.name for p in BUILDS.iterdir() if (p / "tileset.json").exists())}, indent=2))
    leaf = by_level.get(0, {"bytes": 0})
    print(f"  build '{build_name}': {len(metas)} tiles, {report['bytes'] / 1e6:.0f} MB "
          f"(leaves {leaf['bytes'] / 1e6:.0f} MB), {report['seconds']}s")
    return report


def _far_lake(out_dir, root_key: Key, extent) -> dict | None:
    """Lake Michigan beyond the built tiles, so the lake reaches the horizon."""
    x0, y0, x1, y1 = root_key.bounds
    lake = load_features((x0, y0, x1, y1))
    lake = lake[(lake["name"] == "Lake Michigan") & lake.geometry.geom_type.isin(("Polygon", "MultiPolygon"))]
    if not len(lake):
        return None
    geom = shapely.make_valid(shapely.clip_by_rect(shapely.make_valid(lake.geometry.iloc[0]), x0, y0, x1, y1))
    geom = shapely.make_valid(geom.simplify(20.0)).difference(extent)
    mb = mesh.MeshBuilder()
    for c in _cells(root_key.bounds, 4000.0):
        for poly in _polys(_clip_cell(geom, c)):
            mesh.add_flat(mb, poly, 0.0)
    arr = mb.arrays()
    if arr is None:
        return None
    arr.pop("_FEATURE_ID_0")
    uri = "tiles/far-lake.glb"
    (out_dir / "tiles").mkdir(exist_ok=True)
    write_glb(out_dir / uri, [{"name": "water", "material": MAT_INDEX["water"], "arrays": arr}],
              [{"name": n, "color": c} for n, c in MATERIALS.items()])
    hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
    return {"boundingVolume": {"box": [x0 + hx, y0 + hy, 0, hx, 0, 0, 0, hy, 0, 0, 0, 1]},
            "geometricError": 0, "content": {"uri": uri}}


def _geometric_error(level: int) -> float:
    return 0.0 if level == 0 else GEOMETRIC_ERROR_L1 * 2 ** (level - 1)


def _node(key: Key, metas: dict, extents: dict) -> dict:
    children = [_node(k, metas, extents) for k in key.children() if k in extents] if key.level else []
    meta = metas.get(key)
    top = max([meta["max_h"] if meta else 1.0] + [c["_top"] for c in children])
    x0, y0, x1, y1 = key.bounds
    hx, hy, hz = (x1 - x0) / 2, (y1 - y0) / 2, top / 2 + 1
    node = {
        "boundingVolume": {"box": [x0 + hx, y0 + hy, top / 2, hx, 0, 0, 0, hy, 0, 0, 0, hz]},
        "geometricError": _geometric_error(key.level),
        "refine": "REPLACE",
    }
    if meta:
        node["content"] = {"uri": meta["uri"]}
    if children:
        node["children"] = children
    for c in children:
        c.pop("_top")
    node["_top"] = top  # subtree height, used by the parent's bounding box and then removed
    return node


def _attribution() -> list[dict]:
    seen, out = set(), []
    for src in load_sources().values():
        if src["attribution"] in seen:
            continue
        seen.add(src["attribution"])
        out.append({"text": src["attribution"], "license": src["license"], "url": src["homepage"]})
    return out
