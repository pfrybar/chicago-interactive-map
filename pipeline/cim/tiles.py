"""Package buildings and ground into 3D Tiles (one GLB per square tile)."""
import json
import math
import shutil
from datetime import datetime, timezone

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import box

from . import mesh
from .areas import area_polygon_local, area_row
from .config import BUILDS, LOCK_FILE, area_dir, load_config, load_sources
from .frame import local_frame
from .gltf import write_glb
from .ground import LAYER_ORDER

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
GROUND_CELL = 125.0


def _tile_key(x: float, y: float, size: float) -> tuple[int, int]:
    return math.floor(x / size), math.floor(y / size)


def build(areas: list[int], build_name: str) -> dict:
    cfg = load_config()
    size = cfg["tile_size"]
    frame = local_frame()
    out_dir = BUILDS / build_name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "tiles").mkdir(parents=True)

    buildings = pd.concat([gpd.read_parquet(area_dir(a) / "buildings.parquet") for a in areas],
                          ignore_index=True)
    buildings = buildings.drop_duplicates("id")
    ground = pd.concat([gpd.read_parquet(area_dir(a) / "ground.parquet") for a in areas],
                       ignore_index=True)
    # Merge each ground layer across areas, keeping the "no overlap" property.
    merged, above = {}, shapely.Polygon()
    for layer in LAYER_ORDER:
        g = shapely.union_all(ground.loc[ground["layer"] == layer, "geometry"].values)
        g = g.difference(above)
        above = shapely.union_all([above, g])
        merged[layer] = g
    extent = shapely.union_all([g for g in merged.values()])

    centers = buildings.geometry.representative_point()
    buildings["tile"] = [_tile_key(p.x, p.y, size) for p in centers]
    minx, miny, maxx, maxy = extent.bounds
    keys = [(i, j)
            for i in range(math.floor(minx / size), math.ceil(maxx / size))
            for j in range(math.floor(miny / size), math.ceil(maxy / size))]

    children, stats = [], {"tiles": 0, "bytes": 0, "triangles": 0}
    for (i, j) in keys:
        square = box(i * size, j * size, (i + 1) * size, (j + 1) * size)
        meshes = []
        for layer in LAYER_ORDER:
            piece = merged[layer].intersection(square)
            if piece.is_empty:
                continue
            mb = mesh.MeshBuilder()
            # Triangulate in small cells: earcut can fail on huge polygons with many
            # holes (the lake, the street network), and smaller triangles shade better.
            for cell in _cells(square, GROUND_CELL):
                sub = shapely.set_precision(piece.intersection(cell), 0.01)
                for poly in _polys(sub):
                    mesh.add_flat(mb, poly, 0.0)
            if (arr := mb.arrays()) is not None:
                arr.pop("_FEATURE_ID_0")
                meshes.append({"name": layer, "material": MAT_INDEX[layer], "arrays": arr})

        tb = buildings[buildings["tile"] == (i, j)]
        max_h = 1.0
        if len(tb):
            mb = mesh.MeshBuilder()
            features = []
            for fid, (_, b) in enumerate(tb.iterrows()):
                for poly in _polys(b.geometry):
                    mesh.add_extrusion(mb, poly, float(b["min_height"]), float(b["height"]), fid)
                features.append(_feature(b))
                max_h = max(max_h, float(b["height"]))
            if (arr := mb.arrays()) is not None:
                meshes.append({"name": "buildings", "material": MAT_INDEX["building"], "arrays": arr,
                               "extras": {"features": features}})
        if not meshes:
            continue

        uri = f"tiles/{i}_{j}.glb"
        nbytes = write_glb(out_dir / uri, meshes,
                           [{"name": n, "color": c} for n, c in MATERIALS.items()])
        stats["tiles"] += 1
        stats["bytes"] += nbytes
        stats["triangles"] += sum(len(m["arrays"]["indices"]) // 3 for m in meshes)
        cx, cy = (i + 0.5) * size, (j + 0.5) * size
        children.append({
            "boundingVolume": {"box": [cx, cy, max_h / 2, size / 2, 0, 0, 0, size / 2, 0, 0, 0, max_h / 2 + 1]},
            "geometricError": 0,
            "content": {"uri": uri},
        })

    hx, hy = (maxx - minx) / 2, (maxy - miny) / 2
    tileset = {
        "asset": {"version": "1.1", "generator": "chicago-interactive-map pipeline"},
        "geometricError": 100000,
        "root": {
            "transform": frame.enu_to_ecef().flatten(order="F").tolist(),
            "boundingVolume": {"box": [minx + hx, miny + hy, 250, hx, 0, 0, 0, hy, 0, 0, 0, 260]},
            "geometricError": 100000,
            "refine": "ADD",
            "children": children,
        },
    }
    (out_dir / "tileset.json").write_text(json.dumps(tileset))
    (out_dir / "frame.json").write_text(json.dumps(frame.to_dict(), indent=2))
    (out_dir / "overview.json").write_text(json.dumps(_overview(areas, buildings, merged, extent)))

    report = {
        "build": build_name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "areas": [{"number": a, "name": area_row(a)["name"]} for a in areas],
        "buildings": int(len(buildings)),
        "height_sources": buildings["height_source"].value_counts().to_dict(),
        "building_kinds": buildings["kind"].value_counts().to_dict(),
        **stats,
        "tile_size_m": size,
        "sources": json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else {},
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    (out_dir / "attribution.json").write_text(json.dumps(_attribution(), indent=2))

    manifest = BUILDS / "manifest.json"
    manifest.write_text(json.dumps({"current": build_name, "builds": sorted(
        p.name for p in BUILDS.iterdir() if (p / "tileset.json").exists())}, indent=2))
    print(f"  build '{build_name}': {stats['tiles']} tiles, {stats['triangles']:,} triangles, "
          f"{stats['bytes'] / 1e6:.1f} MB")
    return report


def _cells(square, size: float):
    x0, y0, x1, y1 = square.bounds
    n = max(1, round((x1 - x0) / size))
    step = (x1 - x0) / n
    return [box(x0 + i * step, y0 + j * step, x0 + (i + 1) * step, y0 + (j + 1) * step)
            for i in range(n) for j in range(n)]


def _polys(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in _polys(g)]
    return []


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


def _rings(geom, tol: float) -> list:
    out = []
    for p in _polys(geom.simplify(tol)):
        out.append([[round(x, 1), round(y, 1)] for x, y in p.exterior.coords])
    return out


def _overview(areas, buildings, merged, extent) -> dict:
    """Simplified 2D shapes for the minimap, in local meters."""
    return {
        "bounds": [round(v, 1) for v in extent.bounds],
        "areas": [{"number": a, "name": area_row(a)["name"],
                   "rings": _rings(area_polygon_local(a), 2.0)} for a in areas],
        "water": _rings(merged["water"], 3.0),
        "green": _rings(merged["green"], 3.0),
        "roads": _rings(merged["road"], 2.0),
        "buildings": [r for g in buildings.geometry for r in _rings(g, 1.0)],
    }


def _attribution() -> list[dict]:
    seen, out = set(), []
    for src in load_sources().values():
        if src["attribution"] in seen:
            continue
        seen.add(src["attribution"])
        out.append({"text": src["attribution"], "license": src["license"], "url": src["homepage"]})
    return out
