"""Water surface elevation per water body.

Lake Michigan and the river system share the lake level (`water_level` in config).
Inland lagoons and ponds don't: Garfield Park Lagoon sits about 5.7 m above the lake,
and drawing it at lake level made a canyon with tall walls. Each mapped water body
smaller than LAKE_AREA gets the median of the lidar's water-class returns inside it
(at least INSET m from the shore). Bodies without enough returns keep the lake level.

Writes <lidar work>/water/bodies.parquet: one row per body with its surface z (local).
"""
from functools import lru_cache

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from .config import load_config, lidar_work
from .frame import local_frame

WATER_CLASS = 9
LAKE_AREA = 1.0e6       # m²; larger bodies (the river system) are at lake level
LAKE_EXTENT = 4.0e6     # m² of bounding box; long canals too
INSET = 5.0
MIN_RETURNS = 30
PATH = lidar_work("water") / "bodies.parquet"


def lake_z() -> float:
    cfg = load_config()
    return float(cfg["water_level"]) - float(cfg["ground_datum"])


def _bodies() -> gpd.GeoDataFrame:
    """Connected mapped water polygons in and around the city, in the local frame."""
    from .osm import load_features
    x0, y0, x1, y1 = load_config()["city_bbox"]
    t = Transformer.from_crs("EPSG:4326", local_frame().crs, always_xy=True)
    xs, ys = t.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    feats = load_features((min(xs), min(ys), max(xs), max(ys)))
    sel = ((feats["natural"] == "water") | (feats["waterway"] == "riverbank")
           | feats["landuse"].isin(["reservoir", "basin"])) & feats.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    merged = shapely.union_all([shapely.make_valid(g) for g in feats[sel].geometry.values])
    polys = [g for g in getattr(merged, "geoms", [merged]) if g.geom_type == "Polygon" and g.area > 50]
    return gpd.GeoDataFrame(geometry=polys, crs=local_frame().crs)


def measure(poly, read=None) -> tuple[float, str, int]:
    """(surface z, source, returns used) for one body."""
    from .lidar import read_points
    x0, y0, x1, y1 = poly.bounds
    if poly.area > LAKE_AREA or (x1 - x0) * (y1 - y0) > LAKE_EXTENT:
        return lake_z(), "lake", 0
    read = read or read_points
    x, y, z, c = read(poly.bounds, load_config()["lidar"]["depth"])
    wet = c == WATER_CLASS
    for inset in (INSET, 2.0):
        inner = poly.buffer(-inset)
        if inner.is_empty:
            continue
        m = wet.copy()
        m[wet] = shapely.contains_xy(inner, x[wet], y[wet])
        if m.sum() >= MIN_RETURNS:
            return float(np.median(z[m])), "lidar", int(m.sum())
    return lake_z(), "lake", 0


def build(force: bool = False) -> gpd.GeoDataFrame:
    """Measure every body, unless the saved levels are newer than the OSM features."""
    from .osm import FEATURES
    if PATH.exists() and not force and PATH.stat().st_mtime > FEATURES.stat().st_mtime:
        return gpd.read_parquet(PATH)
    bodies = _bodies()
    rows = [measure(g) for g in bodies.geometry]
    bodies["z"] = [r[0] for r in rows]
    bodies["source"] = [r[1] for r in rows]
    bodies["returns"] = [r[2] for r in rows]
    PATH.parent.mkdir(parents=True, exist_ok=True)
    bodies.to_parquet(PATH)
    inland = bodies[bodies["source"] == "lidar"]
    off = inland["z"] - lake_z()
    print(f"  water: {len(bodies)} bodies, {len(inland)} measured from lidar "
          f"({int((off.abs() > 1).sum())} more than 1 m from lake level)")
    return bodies


@lru_cache
def _index():
    bodies = gpd.read_parquet(PATH) if PATH.exists() else gpd.GeoDataFrame(geometry=[], crs=local_frame().crs)
    return shapely.STRtree(bodies.geometry.values), bodies["z"].to_numpy() if len(bodies) else np.zeros(0)


def level_at(poly) -> float:
    """Surface z for a piece of water: its body's level, or the lake's."""
    tree, z = _index()
    if not len(z):
        return lake_z()
    p = poly.representative_point()
    hit = tree.query(p, predicate="within")
    return float(z[hit[0]]) if len(hit) else lake_z()
