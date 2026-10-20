"""Cut Chicago out of the Illinois OSM extract and keep only the features we use."""
import json
import subprocess

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import shape

from .config import WORK, load_config
from .frame import local_frame
from .sources import raw_path

OSM_DIR = WORK / "osm"
FEATURES = OSM_DIR / "features.parquet"

# OSM tags carried into features.parquet. Anything else is dropped.
KEEP_TAGS = [
    "name", "building", "building:part", "height", "min_height", "building:levels",
    "building:min_level", "roof:levels", "chicago:building_id", "location",
    "highway", "area", "area:highway", "tunnel", "bridge", "layer", "covered", "width", "lanes",
    "footway", "natural", "water", "waterway", "leisure", "landuse", "railway", "amenity",
]

TAG_FILTERS = [
    "nwr/building", "nwr/building:part", "w/highway", "nwr/area:highway", "nwr/natural",
    "nwr/waterway", "nwr/water", "nwr/leisure", "nwr/landuse", "nwr/railway",
]


def _run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build_features() -> None:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    w, s, e, n = cfg["city_bbox"]
    city = OSM_DIR / "chicago.osm.pbf"
    filtered = OSM_DIR / "chicago-filtered.osm.pbf"
    seq = OSM_DIR / "features.geojsonseq"

    _run(["osmium", "extract", "--overwrite", "--strategy", "smart", "-b", f"{w},{s},{e},{n}",
          "-o", str(city), str(raw_path("osm_illinois"))])
    _run(["osmium", "tags-filter", "--overwrite", "-o", str(filtered), str(city), *TAG_FILTERS])
    _run(["osmium", "export", "--overwrite", "-f", "geojsonseq", "-a", "type,id",
          "--geometry-types=linestring,polygon",
          "--format-option", "print_record_separator=false",
          "-o", str(seq), str(filtered)])

    records, geoms = [], []
    with open(seq) as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            props = feat["properties"]
            rec = {"osm_id": f"{props['@type'][0]}{props['@id']}"}
            rec.update({k: props.get(k) for k in KEEP_TAGS})
            records.append(rec)
            geoms.append(shape(feat["geometry"]))
    gdf = gpd.GeoDataFrame(pd.DataFrame(records), geometry=geoms, crs="EPSG:4326")
    gdf = gdf.to_crs(local_frame().crs)
    # Sort by 1 km cell and use small row groups, so reading one area's bounding
    # box only touches the row groups nearby (via the bbox covering column).
    c = gdf.geometry.representative_point()
    gdf = gdf.iloc[np.lexsort(((c.y // 1000).values, (c.x // 1000).values))]
    gdf.to_parquet(FEATURES, write_covering_bbox=True, row_group_size=20_000)
    seq.unlink()
    print(f"  {len(gdf):,} OSM features → {FEATURES.relative_to(WORK.parent.parent)}")


def load_features(bounds=None) -> gpd.GeoDataFrame:
    """Features in the local frame whose bounding box overlaps (minx, miny, maxx, maxy)."""
    if bounds is None:
        gdf = gpd.read_parquet(FEATURES)
    else:
        gdf = gpd.read_parquet(FEATURES, bbox=tuple(bounds)).reset_index(drop=True)
    # Missing tags come back as NaN; make them None so `row.get(tag)` checks are simple.
    tags = [c for c in gdf.columns if c != "geometry"]
    gdf[tags] = gdf[tags].astype(object).where(gdf[tags].notna(), None)
    return gdf
