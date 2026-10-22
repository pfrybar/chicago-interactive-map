"""Hand and agent fixes to the map, one YAML file per community area.

data/overrides/<area>.yaml (for example data/overrides/32.yaml for the Loop):

    buildings:
      chi:880169:                       # a building id (chi:<city id> or osm:<w|r><id>)
        height: 12.5                    # meters above its ground (top)
        min_height: 10.5                # optional: bottom, for floating parts (roof slabs, skybridges)
        reason: Lidar caught a crane on the roof
        evidence: mapillary:123456789   # the image or source that shows it
      osm:w590536152:
        hide: true                      # don't draw it
        reason: Duplicates the Willis Tower parts
      chi:366904:
        flat_roof: true                 # extrude instead of using the lidar roof shape
        reason: roofer split the roof into nonsense
    add:
      - id: fix:32:1                    # any unique id starting with "fix:"
        footprint: [[-512.3, 401.2], [-498.0, 401.2], [-498.0, 389.9], [-512.3, 389.9]]
        height: 6
        reason: Garage missing from OSM and the city data
        evidence: kartaview:987654

Coordinates are local meters (x east, y north from State & Madison); use
`python -m cim locate <lat> <lon>`. Every entry needs a `reason`.
"""
import geopandas as gpd
import pandas as pd
import yaml
from shapely.geometry import Polygon

from .config import DATA

OVERRIDES = DATA / "overrides"
FIELDS = {"height", "min_height", "hide", "flat_roof", "reason", "evidence"}


class OverrideError(ValueError):
    pass


def load(area: int) -> dict:
    path = OVERRIDES / f"{area}.yaml"
    if not path.exists():
        return {"buildings": {}, "add": []}
    doc = yaml.safe_load(path.read_text()) or {}
    unknown = set(doc) - {"buildings", "add"}
    if unknown:
        raise OverrideError(f"{path.name}: unknown sections {sorted(unknown)}")
    buildings = doc.get("buildings") or {}
    for bid, fix in buildings.items():
        if not isinstance(fix, dict) or not fix.get("reason"):
            raise OverrideError(f"{path.name}: {bid} needs a reason")
        bad = set(fix) - FIELDS
        if bad:
            raise OverrideError(f"{path.name}: {bid} has unknown fields {sorted(bad)}")
        if "min_height" in fix and ("height" not in fix or float(fix["min_height"]) >= float(fix["height"])):
            raise OverrideError(f"{path.name}: {bid}: min_height needs a height above it")
    adds = doc.get("add") or []
    for a in adds:
        if not str(a.get("id", "")).startswith("fix:") or not a.get("reason") or "footprint" not in a:
            raise OverrideError(f"{path.name}: each added building needs an id starting with 'fix:', "
                                "a footprint and a reason")
    return {"buildings": buildings, "add": adds}


def apply(buildings: gpd.GeoDataFrame, areas: list[int], dem_at) -> gpd.GeoDataFrame:
    """Apply the overrides for `areas`. `dem_at(x, y)` gives ground height for added buildings."""
    b = buildings
    hidden, missing, added = [], [], []
    for area in areas:
        o = load(area)
        for bid, fix in o["buildings"].items():
            rows = b.index[b["id"] == bid]
            if not len(rows):
                missing.append(bid)
                continue
            if fix.get("hide"):
                hidden.extend(rows)
                continue
            if "height" in fix:
                b.loc[rows, "height"] = float(fix["height"])
                b.loc[rows, "height_source"] = "override"
                if "min_height" in fix:
                    b.loc[rows, "min_height"] = float(fix["min_height"])
                else:  # a top below the mapped bottom would turn the part inside out
                    b.loc[rows, "min_height"] = b.loc[rows, "min_height"].clip(upper=float(fix["height"]) - 0.5).clip(lower=0)
            if fix.get("flat_roof"):
                b.loc[rows, "flat_roof"] = True
        for a in o["add"]:
            poly = Polygon(a["footprint"])
            c = poly.representative_point()
            added.append({
                "id": a["id"], "osm_id": None, "kind": "fix", "name": a.get("name"),
                "height": float(a.get("height", 6.0)), "min_height": 0.0, "height_source": "override",
                "year_built": None, "geometry": poly, "area": area, "cx": c.x, "cy": c.y,
                "fp_area": poly.area, "base": float(dem_at(c.x, c.y)),
                "height_prev": float(a.get("height", 6.0)), "source_prev": "override",
            })
    if hidden:
        b = b.drop(index=hidden)
    if added:
        b = pd.concat([b, gpd.GeoDataFrame(added, geometry="geometry", crs=b.crs)], ignore_index=True)
        b = gpd.GeoDataFrame(b, geometry="geometry", crs=buildings.crs)
    if missing:
        print(f"  overrides: {len(missing)} ids not found (stale?): {', '.join(missing[:5])}")
    if hidden or added or "override" in set(b["height_source"]):
        print(f"  overrides: {int((b['height_source'] == 'override').sum())} heights set, "
              f"{len(hidden)} hidden, {len(added)} added")
    return b
