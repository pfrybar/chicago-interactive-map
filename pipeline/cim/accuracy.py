"""Accuracy report: how the pre-lidar heights compare with lidar, and what the map is missing.

Written to data/builds/<build>/accuracy.json. The per-area lists are the starting
point for the neighborhood walk's verify pass (see AGENTS.md).
"""
import json

import numpy as np
import pandas as pd

from .areas import area_row
from .lidar import block_paths

TOP = 15  # items listed per area


def _summary(d: pd.Series) -> dict:
    a = d.abs()
    return {"n": int(len(d)), "median_abs_m": round(float(a.median()), 2),
            "p90_abs_m": round(float(a.quantile(0.9)), 2), "bias_m": round(float(d.median()), 2),
            "within_3m": round(float((a <= 3).mean()), 3)}


def report(buildings: pd.DataFrame, blocks, out_path) -> dict:
    b = buildings
    lid = b[b["height_source"] == "lidar"]
    diff = lid["height_prev"] - lid["height"]
    density = b["n"].fillna(0) / b["fp_area"].clip(lower=1)
    # Footprints of real size where the lidar sees nothing above the ground: maybe demolished
    # (the survey is from spring 2017), maybe under trees, maybe mapped wrong.
    empty = b[(b["fp_area"] > 100) & (density < 0.05) & (b["min_height"] < 0.5)]

    gaps = [pd.read_parquet(block_paths(k)[2]) for k in blocks if block_paths(k)[2].exists()]
    gaps = pd.concat(gaps, ignore_index=True) if gaps else pd.DataFrame(columns=["x", "y", "area"])

    out = {
        "overall": {
            "buildings": int(len(b)),
            "lidar_heights": int(len(lid)),
            "lidar_share": round(len(lid) / max(len(b), 1), 3),
            "previous_vs_lidar": {src: _summary(diff[lid["source_prev"] == src])
                                  for src in sorted(lid["source_prev"].dropna().unique())},
            "no_lidar_returns": int(len(empty)),
            "kept_mapped_newer_than_lidar": int(b.get("newer_than_lidar", pd.Series(dtype=bool)).sum()),
            "unmapped_clusters": int(len(gaps)),
            "unmapped_area_m2": round(float(gaps["area"].sum()), 0),
        },
        "areas": {},
    }

    from .areas import load_areas
    areas = load_areas().to_crs(b.crs) if hasattr(b, "crs") and b.crs else None
    for number in sorted(b["area"].dropna().unique()):
        number = int(number)
        ab = b[b["area"] == number]
        al = ab[ab["height_source"] == "lidar"]
        ad = al["height_prev"] - al["height"]
        worst = al.assign(d=ad).loc[lambda t: t["d"].abs() > 10].sort_values("d", key=np.abs, ascending=False)
        entry = {
            "name": area_row(number)["name"],
            "buildings": int(len(ab)),
            "lidar_share": round(len(al) / max(len(ab), 1), 3),
            "previous_vs_lidar": _summary(ad) if len(ad) else None,
            "largest_disagreements": [
                {"id": r.id, "name": r.name if isinstance(r.name, str) else None,
                 "previous_m": round(float(r.height_prev), 1), "previous_source": r.source_prev,
                 "lidar_m": round(float(r.height), 1), "osm": r.osm_id if isinstance(r.osm_id, str) else None,
                 "at": [round(float(r.cx)), round(float(r.cy))]}
                for r in worst.head(TOP).itertuples()],
            "kept_mapped_newer_than_lidar": [
                {"id": r.id, "name": r.name if isinstance(r.name, str) else None, "mapped_m": round(float(r.height), 1),
                 "source": r.height_source, "osm": r.osm_id if isinstance(r.osm_id, str) else None,
                 "at": [round(float(r.cx)), round(float(r.cy))]}
                for r in ab[ab.get("newer_than_lidar", False) == True].itertuples()],  # noqa: E712
            "no_lidar_returns": [
                {"id": r.id, "osm": r.osm_id if isinstance(r.osm_id, str) else None,
                 "area_m2": round(float(r.fp_area)), "at": [round(float(r.cx)), round(float(r.cy))]}
                for r in empty[empty["area"] == number].sort_values("fp_area", ascending=False).head(TOP).itertuples()],
        }
        if areas is not None and len(gaps):
            poly = areas[areas["number"] == number].geometry.iloc[0]
            import shapely
            inside = gaps[shapely.contains_xy(poly, gaps["x"].astype(float).values, gaps["y"].astype(float).values)]
            entry["unmapped_clusters"] = int(len(inside))
            entry["largest_unmapped"] = [{"at": [round(r.x), round(r.y)], "area_m2": round(r.area)}
                                         for r in inside.sort_values("area", ascending=False).head(TOP).itertuples()]
        out["areas"][str(number)] = entry

    out_path.write_text(json.dumps(out, indent=1))
    o = out["overall"]
    print(f"  accuracy: lidar heights for {o['lidar_heights']:,} of {o['buildings']:,} buildings "
          f"({100 * o['lidar_share']:.0f}%), {o['unmapped_clusters']:,} unmapped clusters, "
          f"{o['no_lidar_returns']:,} footprints with no returns")
    for src, s in o["previous_vs_lidar"].items():
        print(f"    {src:13s} n={s['n']:7,d}  median |Δ| {s['median_abs_m']:5.1f} m  bias {s['bias_m']:+5.1f} m  "
              f"within 3 m {100 * s['within_3m']:.0f}%")
    return out
