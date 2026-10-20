"""Ground layers (streets, paths, water, parks, land) around one community area.

Layers never overlap: each one has everything above it in LAYER_ORDER cut out,
so the whole ground can sit at height 0 with no z-fighting.
"""
import math

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import linemerge, polygonize, unary_union

from .areas import area_polygon_local
from .config import area_dir, load_config
from .frame import local_frame
from .osm import load_features

# Top of the stack first.
LAYER_ORDER = ["path", "road", "water", "sand", "green", "land"]

ROAD_WIDTH = {
    "motorway": 14, "trunk": 13, "primary": 13, "secondary": 11, "tertiary": 10,
    "unclassified": 8, "residential": 8, "living_street": 6, "service": 5,
    "motorway_link": 7, "trunk_link": 7, "primary_link": 7, "secondary_link": 6, "tertiary_link": 6,
    "busway": 7,
}
PATH_WIDTH = {"footway": 2.5, "pedestrian": 5, "path": 2, "cycleway": 2, "steps": 2, "corridor": 2}
GREEN = {
    "leisure": {"park", "garden", "pitch", "playground", "nature_reserve", "golf_course", "dog_park",
                "recreation_ground", "common"},
    "landuse": {"grass", "recreation_ground", "meadow", "cemetery", "village_green", "forest", "flowerbed"},
    "natural": {"wood", "scrub", "grassland", "heath", "wetland"},
}


def tile_extent(area: int) -> Polygon:
    """Square region covering the area's tiles plus a margin of tiles."""
    cfg = load_config()
    size, margin = cfg["tile_size"], cfg["ground_margin_tiles"]
    minx, miny, maxx, maxy = area_polygon_local(area).bounds
    x0 = (math.floor(minx / size) - margin) * size
    y0 = (math.floor(miny / size) - margin) * size
    x1 = (math.floor(maxx / size) + 1 + margin) * size
    y1 = (math.floor(maxy / size) + 1 + margin) * size
    return box(x0, y0, x1, y1)


def _width(row, table) -> float:
    w = row.get("width")
    try:
        if w:
            return max(float(str(w).split()[0]), 1.0)
    except ValueError:
        pass
    lanes = row.get("lanes")
    try:
        if lanes and row["highway"] in ROAD_WIDTH:
            return max(float(str(lanes).split(";")[0]) * 3.3, 4.0)
    except ValueError:
        pass
    return table[row["highway"]]


def _is_lower_level(row) -> bool:
    if row.get("tunnel") not in (None, "no") or row.get("covered") == "yes":
        return True
    try:
        return int(str(row.get("layer") or 0).split(";")[0]) < 0
    except ValueError:
        return False


def _buffer_lines(gdf, table) -> shapely.Geometry:
    parts = []
    for _, r in gdf.iterrows():
        if _is_lower_level(r):
            continue
        parts.append(r.geometry.buffer(_width(r, table) / 2, cap_style="flat", join_style="round",
                                       quad_segs=2))
    return unary_union(parts) if parts else Polygon()


def _union_polys(gdf) -> shapely.Geometry:
    polys = gdf[gdf.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry.make_valid()
    return unary_union(list(polys)) if len(polys) else Polygon()


def lake_from_coastline(coast_lines, extent: Polygon) -> shapely.Geometry:
    """Water side of the OSM coastline inside `extent`.

    OSM coastlines run with land on the left and water on the right.
    """
    lines = [g for g in coast_lines if g.intersects(extent)]
    if not lines:
        return Polygon()
    merged = linemerge(unary_union(lines))
    clipped = merged.intersection(extent.buffer(1.0))
    faces = list(polygonize(unary_union([extent.exterior, clipped])))
    segs = []
    for line in getattr(merged, "geoms", [merged]):
        c = np.asarray(line.coords)
        segs.append(np.stack([c[:-1], c[1:]], axis=1))
    segs = np.concatenate(segs)
    water = []
    for face in faces:
        if face.area < 1:
            continue
        p = np.asarray(face.representative_point().coords[0])
        a, b = segs[:, 0], segs[:, 1]
        ab = b - a
        t = np.clip(np.einsum("ij,ij->i", p - a, ab) / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-9), 0, 1)
        d = np.linalg.norm(a + ab * t[:, None] - p, axis=1)
        i = int(np.argmin(d))
        cross = ab[i, 0] * (p[1] - a[i, 1]) - ab[i, 1] * (p[0] - a[i, 0])
        if cross < 0:  # right of the coastline: water
            water.append(face)
    return unary_union(water) if water else Polygon()


def build(area: int) -> gpd.GeoDataFrame:
    extent = tile_extent(area)
    feats = load_features(extent.buffer(100).bounds)
    feats = feats[feats.intersects(extent)]
    hw = feats["highway"]

    roads = _buffer_lines(feats[hw.isin(ROAD_WIDTH.keys()) & (feats.geometry.geom_type == "LineString")],
                          ROAD_WIDTH)
    paths = _buffer_lines(feats[hw.isin(PATH_WIDTH.keys()) & (feats.geometry.geom_type == "LineString")],
                          PATH_WIDTH)
    plazas = _union_polys(feats[(hw == "pedestrian") | feats["area:highway"].notna()])

    water = unary_union([
        _union_polys(feats[(feats["natural"] == "water") | (feats["waterway"] == "riverbank")
                           | feats["landuse"].isin(["reservoir", "basin"])]),
        lake_from_coastline(feats[feats["natural"] == "coastline"].geometry, extent),
    ])
    sand = _union_polys(feats[feats["natural"].isin(["beach", "sand"])])
    green = _union_polys(feats[
        feats["leisure"].isin(GREEN["leisure"]) | feats["landuse"].isin(GREEN["landuse"])
        | feats["natural"].isin(GREEN["natural"])
    ])

    raw = {
        "path": unary_union([paths, plazas]),
        "road": roads,
        "water": water,
        "sand": sand,
        "green": green,
        "land": extent,
    }
    rows, above = [], Polygon()
    for layer in LAYER_ORDER:
        geom = raw[layer].intersection(extent).difference(above).buffer(0)
        above = unary_union([above, geom])
        geom = _clean(geom)
        if not geom.is_empty:
            rows.append({"layer": layer, "geometry": geom})

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=local_frame().crs)
    dest = area_dir(area) / "ground.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dest)
    print(f"  area {area}: ground layers {', '.join(out['layer'])} over "
          f"{extent.area / 1e6:.1f} km²")
    return out


def _clean(geom) -> MultiPolygon:
    """Keep polygon pieces only and drop slivers."""
    polys = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon) and g.area > 0.5]
    for g in getattr(geom, "geoms", []):
        if isinstance(g, MultiPolygon):
            polys.extend(p for p in g.geoms if p.area > 0.5)
    return MultiPolygon(polys) if polys else MultiPolygon()
