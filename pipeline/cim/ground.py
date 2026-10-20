"""Ground layers: streets, paths, water, sand, parks and plain land.

Layers never overlap: each has everything above it in LAYER_ORDER cut out,
so the whole ground can sit at height 0 with no z-fighting.
"""
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

import numpy as np

# Top of the stack first. "land" is whatever is left.
LAYER_ORDER = ["path", "road", "rail", "water", "sand", "green", "land"]

ROAD_WIDTH = {
    "motorway": 14, "trunk": 13, "primary": 13, "secondary": 11, "tertiary": 10,
    "unclassified": 8, "residential": 8, "living_street": 6, "service": 5,
    "motorway_link": 7, "trunk_link": 7, "primary_link": 7, "secondary_link": 6, "tertiary_link": 6,
    "busway": 7,
}
MAJOR_ROADS = {"motorway", "trunk", "primary", "secondary", "motorway_link", "trunk_link"}
PATH_WIDTH = {"footway": 2.5, "pedestrian": 5, "path": 2, "cycleway": 2, "steps": 2, "corridor": 2}
GREEN = {
    "leisure": {"park", "garden", "pitch", "playground", "nature_reserve", "golf_course", "dog_park",
                "recreation_ground", "common"},
    "landuse": {"grass", "recreation_ground", "meadow", "cemetery", "village_green", "forest", "flowerbed"},
    "natural": {"wood", "scrub", "grassland", "heath", "wetland"},
}

# What each tile level draws. Level 0 is full detail; higher levels are seen from farther away.
LEVEL_LAYERS = {0: ["path", "road", "rail", "water", "sand", "green"],
                1: ["road_major", "rail", "water", "sand", "green"],
                2: ["road_major", "water", "green"]}
RAILBED_WIDTH = 4.0  # per track, for railways at ground level
UPPER_LAYERS = ["water", "green"]


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


def _union_polys(gdf, extent) -> shapely.Geometry:
    polys = gdf[gdf.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry
    if not len(polys):
        return Polygon()
    clipped = [shapely.make_valid(shapely.clip_by_rect(shapely.make_valid(g), *extent.bounds)) for g in polys]
    return unary_union(clipped)


def _railbeds(feats) -> shapely.Geometry:
    """Gravel strips under railways at ground level (bridges and tunnels excluded)."""
    from .rail import railways
    r = railways(feats)
    r = r[~r["elevated"]]
    if not len(r):
        return Polygon()
    return unary_union(list(r.geometry.buffer(RAILBED_WIDTH / 2, cap_style="flat", quad_segs=2)))


def lake_from_coastline(coast_lines, extent: Polygon) -> shapely.Geometry:
    """Water side of the OSM coastline inside `extent`.

    OSM coastlines run with land on the left and water on the right. (Around
    Chicago, Lake Michigan is also mapped as a water polygon, which is what
    usually supplies it; this covers areas where only the coastline exists.)
    """
    lines = [g for g in coast_lines if g.intersects(extent)]
    if not lines:
        return Polygon()
    merged = shapely.line_merge(unary_union(lines))
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


def raw_layers(feats, extent: Polygon) -> dict[str, shapely.Geometry]:
    """Every ground layer inside `extent`, before overlaps are removed."""
    hw = feats["highway"]
    lines = feats.geometry.geom_type == "LineString"
    roads = feats[hw.isin(ROAD_WIDTH.keys()) & lines]
    on_bridge = feats["bridge"].notna() & (feats["bridge"] != "no") & lines
    return {
        # Not drawn: where walls along the water must stay open (under bridges).
        "bridge": unary_union([
            g.buffer(_width(r, ROAD_WIDTH if r["highway"] in ROAD_WIDTH else PATH_WIDTH) / 2 + 1.0, cap_style="flat")
            for _, r in feats[on_bridge & (hw.isin(ROAD_WIDTH.keys()) | hw.isin(PATH_WIDTH.keys()))].iterrows()
            for g in [r.geometry]
        ] + [feats[on_bridge & feats["railway"].notna()].geometry.buffer(5, cap_style="flat").union_all()]
          if on_bridge.any() else []),
        "road": _buffer_lines(roads, ROAD_WIDTH),
        "rail": _railbeds(feats),
        "road_major": _buffer_lines(roads[roads["highway"].isin(MAJOR_ROADS)], ROAD_WIDTH),
        "path": unary_union([
            _buffer_lines(feats[hw.isin(PATH_WIDTH.keys()) & lines], PATH_WIDTH),
            _union_polys(feats[(hw == "pedestrian") | feats["area:highway"].notna()], extent),
        ]),
        "water": unary_union([
            _union_polys(feats[(feats["natural"] == "water") | (feats["waterway"] == "riverbank")
                               | feats["landuse"].isin(["reservoir", "basin"])], extent),
            lake_from_coastline(feats[feats["natural"] == "coastline"].geometry, extent),
        ]),
        "sand": _union_polys(feats[feats["natural"].isin(["beach", "sand"])], extent),
        "green": _union_polys(feats[
            feats["leisure"].isin(GREEN["leisure"]) | feats["landuse"].isin(GREEN["landuse"])
            | feats["natural"].isin(GREEN["natural"])
        ], extent),
    }


def stack(raw: dict, extent: Polygon, layers: list[str], simplify: float = 0.0) -> dict[str, MultiPolygon]:
    """Clip `layers` (top first) to `extent`, cut out what lies above, and fill the rest with land.

    The result also carries "bridge" (not drawn) when the raw layers have it.
    """
    out, above = {}, Polygon()
    if raw.get("bridge") is not None and not raw["bridge"].is_empty:
        out["bridge"] = shapely.make_valid(shapely.clip_by_rect(raw["bridge"], *extent.bounds))
    for layer in layers:
        g = raw.get(layer)
        if g is None or g.is_empty:
            continue
        g = shapely.make_valid(shapely.clip_by_rect(g, *extent.bounds))
        if simplify:
            g = shapely.make_valid(g.simplify(simplify))
        g = g.difference(above)
        above = unary_union([above, g])
        g = _clean(g)
        if not g.is_empty:
            out["road" if layer == "road_major" else layer] = g
    land = _clean(extent.difference(above))
    if not land.is_empty:
        out["land"] = land
    return out


def _clean(geom) -> MultiPolygon:
    """Keep polygon pieces only and drop slivers."""
    polys = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon) and g.area > 0.5:
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(p for p in g.geoms if p.area > 0.5)
    return MultiPolygon(polys) if polys else MultiPolygon()
