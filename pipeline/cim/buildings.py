"""Buildings for one community area: OSM footprints joined with City of Chicago attributes."""
import geopandas as gpd
import pandas as pd

from . import heights
from .areas import area_polygon_local
from .config import area_dir
from .frame import local_frame
from .osm import load_features
from .sources import raw_path

POLY = ("Polygon", "MultiPolygon")


def _polygons(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf[gdf.geometry.geom_type.isin(POLY)].copy()
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf.explode(index_parts=False)
    gdf = gdf[gdf.geometry.geom_type == "Polygon"]
    return gdf[gdf.geometry.area > 1.0]


def _load_city(area: int) -> gpd.GeoDataFrame:
    city = gpd.read_file(raw_path("city_footprints", area))
    city = city.to_crs(local_frame().crs)
    city["bldg_id"] = city["bldg_id"].astype(str)
    return city


def build(area: int) -> gpd.GeoDataFrame:
    boundary = area_polygon_local(area)
    feats = load_features(boundary.buffer(200).bounds)
    feats = feats[feats.intersects(boundary.buffer(200))]

    underground = feats["location"].isin(["underground", "underwater"])
    is_part = feats["building:part"].notna() & (feats["building:part"] != "no")
    is_outline = feats["building"].notna() & (feats["building"] != "no") & ~is_part
    outlines = _polygons(feats[is_outline & ~underground])
    parts = _polygons(feats[is_part & ~underground])

    # Simple 3D Buildings rule: an outline that contains parts is drawn by its parts.
    if len(parts):
        pts = gpd.GeoDataFrame(geometry=parts.geometry.representative_point(), crs=parts.crs)
        hit = gpd.sjoin(pts, outlines[["geometry"]], predicate="within", how="inner")
        outlines = outlines[~outlines.index.isin(hit["index_right"].unique())]

    city = _load_city(area)
    match = _match_city(outlines, city)
    osm_city_ids = {rec["bldg_id"] for rec in match.values()}

    rows = []
    for kind, gdf in (("outline", outlines), ("part", parts)):
        for idx, r in gdf.iterrows():
            tags = r.to_dict()
            city_rec = match.get(idx) if kind == "outline" else None
            cid = city_rec["bldg_id"] if city_rec is not None else None
            h, mh, src = heights.resolve(tags, city_rec["stories"] if city_rec is not None else None,
                                         r.geometry.area)
            rows.append({
                "id": f"chi:{cid}" if city_rec is not None else f"osm:{r['osm_id']}",
                "osm_id": r["osm_id"],
                "kind": kind,
                "name": tags.get("name") or (city_rec["bldg_name1"] if city_rec is not None else None) or None,
                "height": round(h, 2),
                "min_height": round(mh, 2),
                "height_source": src,
                "year_built": _year(city_rec["year_built"]) if city_rec is not None else None,
                "geometry": r.geometry,
            })

    # City footprints that OSM doesn't have yet.
    osm_all = pd.concat([outlines.geometry, parts.geometry])
    osm_index = gpd.GeoDataFrame(geometry=osm_all.values, crs=city.crs)
    city_polys = _polygons(city[~city["bldg_id"].isin(osm_city_ids)])
    if len(city_polys):
        pts = gpd.GeoDataFrame(city_polys[["bldg_id"]], geometry=city_polys.geometry.representative_point(),
                               crs=city.crs)
        covered = gpd.sjoin(pts, osm_index, predicate="within", how="inner").index.unique()
        for idx, r in city_polys[~city_polys.index.isin(covered)].iterrows():
            h, mh, src = heights.resolve({}, r["stories"], r.geometry.area)
            rows.append({
                "id": f"chi:{r['bldg_id']}", "osm_id": None, "kind": "city",
                "name": r["bldg_name1"] or None, "height": round(h, 2), "min_height": 0.0,
                "height_source": src, "year_built": _year(r["year_built"]), "geometry": r.geometry,
            })

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=local_frame().crs)
    # Keep buildings whose center is inside this community area.
    inside = out.geometry.representative_point().within(boundary)
    out = out[inside].reset_index(drop=True)
    out["id"] = _dedupe(out["id"])

    dest = area_dir(area) / "buildings.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dest)
    counts = out["height_source"].value_counts().to_dict()
    print(f"  area {area}: {len(out):,} buildings/parts; height sources {counts}")
    return out


def _match_city(outlines: gpd.GeoDataFrame, city: gpd.GeoDataFrame) -> dict:
    """Pair OSM outlines with city footprints.

    First by the `chicago:building_id` tag left by the 2012–13 import, then, for
    outlines without a usable tag, by the largest city footprint whose center
    falls inside the outline.
    """
    by_id = city.drop_duplicates("bldg_id").set_index("bldg_id")
    match = {}
    for idx, cid in outlines["chicago:building_id"].items():
        if cid is not None and cid in by_id.index:
            match[idx] = by_id.loc[cid].to_dict() | {"bldg_id": cid}
    rest = outlines[~outlines.index.isin(match.keys())]
    if len(rest) and len(city):
        pts = gpd.GeoDataFrame(city.drop(columns="geometry"), geometry=city.geometry.representative_point(),
                               crs=city.crs)
        pts["fp_area"] = city.geometry.area
        hit = gpd.sjoin(pts, rest[["geometry"]], predicate="within", how="inner")
        hit = hit.sort_values("fp_area", ascending=False).drop_duplicates("index_right")
        for _, h in hit.iterrows():
            match[h["index_right"]] = h.drop(["geometry", "index_right"]).to_dict()
    return match


def _year(v):
    try:
        y = int(float(v))
    except (TypeError, ValueError):
        return None
    return y if y > 1800 else None


def _dedupe(ids: pd.Series) -> pd.Series:
    """Multipolygon footprints explode into several rows; suffix repeats so ids stay unique."""
    seen: dict[str, int] = {}
    out = []
    for i in ids:
        n = seen.get(i, 0)
        out.append(i if n == 0 else f"{i}#{n}")
        seen[i] = n + 1
    return pd.Series(out, index=ids.index)
