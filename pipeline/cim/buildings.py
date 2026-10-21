"""Buildings for one community area: OSM footprints joined with City of Chicago attributes."""
import geopandas as gpd
import pandas as pd
import shapely

from . import heights
from .areas import area_polygon_local
from .config import area_dir
from .frame import local_frame
from .osm import load_features
from .sources import raw_path

POLY = ("Polygon", "MultiPolygon")
PARTS_COVER = 0.9  # parts covering at least this share of an outline replace it


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
    # Construction sites aren't buildings yet (OSM's building=construction).
    is_outline = feats["building"].notna() & ~feats["building"].isin(["no", "construction"]) & ~is_part
    # Unique indexes: exploded multipolygons repeat their source row's label.
    outlines = _polygons(feats[is_outline & ~underground]).reset_index(drop=True)
    parts = _polygons(feats[is_part & ~underground]).reset_index(drop=True)

    city = _load_city(area)
    match = _match_city(outlines, city)
    osm_city_ids = {rec["bldg_id"] for rec in match.values()}

    # Simple 3D Buildings rule: an outline that contains parts is drawn by its parts,
    # and the parts inherit the outline's name and city record. Mappers often draw
    # only a few parts (the Art Institute has two), so an outline is dropped only when
    # its parts cover most of it; otherwise what the parts don't cover stays.
    parent: dict = {}
    parent_name: dict = {}
    trimmed: dict = {}
    if len(parts):
        pts = gpd.GeoDataFrame(geometry=parts.geometry.representative_point(), crs=parts.crs)
        hit = gpd.sjoin(pts, outlines[["geometry"]], predicate="within", how="inner")
        parent = hit.groupby(level=0)["index_right"].first().to_dict()
        names = outlines["name"]
        parent_name = {k: names.get(v) for k, v in parent.items()}
        drop = []
        for oidx, part_idx in hit.groupby("index_right").groups.items():
            outline = outlines.geometry.loc[oidx]
            covered = shapely.union_all(parts.loc[list(set(part_idx))].geometry.values)
            rest = shapely.make_valid(outline.difference(covered))
            if rest.area <= (1 - PARTS_COVER) * outline.area:
                drop.append(oidx)
            else:
                trimmed[oidx] = rest
        outlines = outlines[~outlines.index.isin(drop)].copy()
        for oidx, rest in trimmed.items():
            if oidx in outlines.index:
                polys = [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "Polygon" and g.area > 1]
                if polys:
                    outlines.loc[oidx, "geometry"] = shapely.MultiPolygon(polys) if len(polys) > 1 else polys[0]
                else:
                    outlines = outlines.drop(index=oidx)

    rows = []
    for kind, gdf in (("outline", outlines), ("part", parts)):
        for idx, r in gdf.iterrows():
            tags = r.to_dict()
            city_rec = match.get(idx if kind == "outline" else parent.get(idx))
            if kind == "part" and not tags.get("name"):
                tags["name"] = parent_name.get(idx)
            cid = city_rec["bldg_id"] if city_rec is not None else None
            # A part's height comes from its own tags; the outline's floor count covers the whole building.
            stories = city_rec["stories"] if city_rec is not None and kind == "outline" else None
            h, mh, src = heights.resolve(tags, stories, r.geometry.area)
            if (tags.get("building") == "roof" or tags.get("building:part") == "roof") and mh < h - 1.0:
                # A roof without walls (canopy, shelter): draw a 1 m slab at the top. Being
                # off the ground also keeps lidar from giving it the height of what's above.
                mh = h - 1.0
            rows.append({
                "id": f"chi:{cid}" if city_rec is not None and kind == "outline" else f"osm:{r['osm_id']}",
                "osm_id": r["osm_id"],
                "kind": kind,
                "name": tags.get("name") or (city_rec["bldg_name1"] if city_rec is not None else None) or None,
                "height": round(h, 2),
                "min_height": round(mh, 2),
                "height_source": src,
                "year_built": _year(city_rec["year_built"]) if city_rec is not None else None,
                "has_parts": False,
                "_outline": idx if kind == "outline" else parent.get(idx),
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
                "height_source": src, "year_built": _year(r["year_built"]), "has_parts": False,
                "_outline": None, "geometry": r.geometry,
            })

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=local_frame().crs)
    # has_parts: the rest of an outline whose tower is one of its mapped parts (a part
    # reaches 90% of the outline's height). Its height tags describe that tower, so the
    # lidar's lower reading of the rest is the podium, not a building newer than the survey.
    is_rest = (out["kind"] == "outline") & out["_outline"].isin(list(trimmed))
    tallest = out[out["kind"] == "part"].groupby("_outline")["height"].max()
    out["has_parts"] = is_rest & (out["_outline"].map(tallest).fillna(0) >= 0.9 * out["height"])
    out = out.drop(columns="_outline")
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
