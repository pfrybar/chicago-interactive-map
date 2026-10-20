"""Chicago's 77 community areas: the units the pipeline builds and the agent walks."""
import re
from functools import lru_cache

import geopandas as gpd

from .frame import local_frame
from .sources import raw_path


def _title(name: str) -> str:
    t = name.title()
    return re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), t)


@lru_cache
def load_areas() -> gpd.GeoDataFrame:
    """Community areas in lon/lat, with integer `number` and display `name`."""
    gdf = gpd.read_file(raw_path("community_areas"))
    gdf = gdf.rename(columns={"area_numbe": "number", "community": "name"})
    gdf["number"] = gdf["number"].astype(int)
    gdf["name"] = gdf["name"].map(_title)
    return gdf[["number", "name", "geometry"]].sort_values("number").reset_index(drop=True)


def area_row(number: int):
    gdf = load_areas()
    rows = gdf[gdf["number"] == number]
    if rows.empty:
        raise SystemExit(f"No community area {number} (valid: 1–77)")
    return rows.iloc[0]


def area_bbox_lonlat(number: int, margin_deg: float = 0.0):
    w, s, e, n = area_row(number).geometry.bounds
    return w - margin_deg, s - margin_deg, e + margin_deg, n + margin_deg


def area_polygon_local(number: int):
    """The area's boundary in the local frame (meters)."""
    gdf = load_areas()
    rows = gdf[gdf["number"] == number].to_crs(local_frame().crs)
    return rows.geometry.iloc[0].buffer(0)
