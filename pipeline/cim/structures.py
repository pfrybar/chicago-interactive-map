"""Road overpasses and ramps as structures: decks with parapets on piers.

Roads were all draped on the ground model, so a flyover became a ridge in the terrain
and stacked ramps one warped surface (the Jane Byrne interchange). Now every mapped
road or path bridge that isn't mostly over water is an overpass:

- its deck height comes from the lidar's bridge-deck returns along it (class 17),
  fitted like the L's track (rail.fit_profile): a robust median with a grade cap;
- it's drawn as a concrete slab with an asphalt top, parapets and piers, and left out
  of the ground layers and the ground model, so whatever passes under it shows.

Bridges over water (the river) stay as before: ground-level roads whose deck points
are in the ground model, so they stay walkable without special cases.

Per block this writes <lidar work>/structures/<i>_<j>.parquet: way, s (chainage), z.
"""
import time
import multiprocessing
import warnings
from concurrent.futures import ProcessPoolExecutor

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import box

from . import mesh
from .config import lidar_work, lidar_workers, load_config
from .grid import Key
from .lidar import block_paths, load_dem, read_points
from .osm import load_features

_FRESH = multiprocessing.get_context("forkserver")

STRUCTURES = lidar_work("structures")
SETTINGS = "v1"
SAMPLE = 4.0          # m between profile samples
DECK_CLASS = 17       # ASPRS bridge deck
WATER_SHARE = 0.5     # bridges with more of their length over water stay ground-level roads
SLAB = 1.2            # m from deck top to soffit
PARAPET = 0.9         # m above the deck
PIER_SPACING = 30.0   # m along the deck
DEFAULT_RISE = 6.0    # m per layer above ground when the lidar has nothing


def _layer(v) -> int:
    try:
        return int(str(v).split(";")[0])
    except (TypeError, ValueError):
        return 0


def _water(feats, bounds) -> shapely.Geometry:
    sel = ((feats["natural"] == "water") | (feats["waterway"] == "riverbank")
           | feats["landuse"].isin(["reservoir", "basin"])) & feats.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    if not sel.any():
        return shapely.Polygon()
    # make_valid and clip_by_rect can return collections with stray lines, which union_all rejects
    parts = shapely.get_parts(shapely.make_valid(shapely.clip_by_rect(shapely.make_valid(feats[sel].geometry.values), *bounds)))
    parts = shapely.get_parts(parts)
    return shapely.union_all(parts[shapely.get_type_id(parts) == 3])


def overpasses(feats, bounds=None) -> gpd.GeoDataFrame:
    """Mapped road and path bridges that aren't mostly over water, with their deck width."""
    from .ground import PATH_WIDTH, ROAD_WIDTH, _is_lower_level, _width
    hw = feats["highway"]
    sel = (feats.geometry.geom_type == "LineString") & feats["bridge"].notna() & (feats["bridge"] != "no") \
        & (hw.isin(ROAD_WIDTH.keys()) | hw.isin(PATH_WIDTH.keys()))
    ways = feats[sel]
    ways = ways[[not _is_lower_level(r) for _, r in ways.iterrows()]]
    if not len(ways):
        return gpd.GeoDataFrame(columns=["osm_id", "width", "layer", "geometry"], geometry="geometry")
    water = _water(feats, bounds or feats.total_bounds)
    if not water.is_empty:
        shapely.prepare(water)
        wet = np.array([g.intersection(water).length / max(g.length, 1e-6) for g in ways.geometry])
        ways = ways[wet < WATER_SHARE]
    width = [_width(r, ROAD_WIDTH if r["highway"] in ROAD_WIDTH else PATH_WIDTH) for _, r in ways.iterrows()]
    return gpd.GeoDataFrame({"osm_id": ways["osm_id"].values, "width": width,
                             "layer": ways["layer"].map(_layer).values}, geometry=ways.geometry.values)


def overpass_area(feats, bounds=None) -> shapely.Geometry:
    """Footprint of the overpass decks (to leave out of the ground layers and model)."""
    ways = overpasses(feats, bounds)
    if not len(ways):
        return shapely.Polygon()
    return shapely.union_all([g.buffer(w / 2, cap_style="flat") for g, w in zip(ways.geometry, ways["width"])])


def _samples(line):
    n = max(2, int(np.ceil(line.length / SAMPLE)) + 1)
    s = np.linspace(0, line.length, n)
    pts = shapely.line_interpolate_point(line, s)
    return s, shapely.get_x(pts), shapely.get_y(pts)


def profile_block(key: Key) -> pd.DataFrame:
    """Deck heights sampled along every overpass in a block, from bridge-deck returns."""
    x0, y0, x1, y1 = key.bounds
    grown = (x0 - 30, y0 - 30, x1 + 30, y1 + 30)
    feats = load_features(grown)
    ways = overpasses(feats, grown)
    ways = ways[ways.intersects(box(*grown))]
    if not len(ways):
        return pd.DataFrame(columns=["way", "s", "z"])
    x, y, z, c = read_points(grown, load_config()["lidar"]["depth"])
    deck = c == DECK_CLASS
    if deck.sum() < 10:
        return pd.DataFrame(columns=["way", "s", "z"])
    tree = cKDTree(np.c_[x[deck], y[deck]])
    zd = z[deck]
    rows = []
    for r in ways.itertuples():
        s, sx, sy = _samples(r.geometry)
        inside = (sx >= x0) & (sx < x1) & (sy >= y0) & (sy < y1)
        if not inside.any():
            continue
        zs = []
        for px, py in zip(sx[inside], sy[inside]):
            near = tree.query_ball_point([px, py], max(1.5, 0.4 * r.width))
            zs.append(float(np.median(zd[near])) if len(near) >= 3 else np.nan)
        zs = np.asarray(zs)
        ok = np.isfinite(zs)
        rows.append(pd.DataFrame({"way": r.osm_id, "s": s[inside][ok], "z": zs[ok]}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["way", "s", "z"])


def _stamp(key: Key):
    return STRUCTURES / f"{key.i}_{key.j}.settings"


def _job(key: Key):
    t0 = time.time()
    df = profile_block(key)
    df.to_parquet(STRUCTURES / f"{key.i}_{key.j}.parquet")
    _stamp(key).write_text(SETTINGS)
    return key, len(df), time.time() - t0


def run(blocks, force: bool = False) -> None:
    STRUCTURES.mkdir(parents=True, exist_ok=True)

    def current(k):
        p = STRUCTURES / f"{k.i}_{k.j}.parquet"
        return p.exists() and _stamp(k).exists() and _stamp(k).read_text() == SETTINGS
    todo = [k for k in sorted(blocks) if (force or not current(k)) and block_paths(k)[0].exists()]
    if not todo:
        print("  structures: all blocks up to date")
        return
    workers = lidar_workers(load_config()["lidar"]["workers"])
    print(f"  structures: profiling overpasses in {len(todo)} blocks, {workers} workers")
    t0 = time.time()
    with ProcessPoolExecutor(mp_context=_FRESH, max_workers=workers) as pool:
        for n, (key, nrows, secs) in enumerate(pool.map(_job, todo), 1):
            if n % 20 == 0 or n == len(todo):
                print(f"    {n}/{len(todo)} blocks, {time.time() - t0:.0f}s")


def load_profiles(keys) -> pd.DataFrame:
    parts = [pd.read_parquet(p) for k in keys if (p := STRUCTURES / f"{k.i}_{k.j}.parquet").exists()]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["way", "s", "z"])


def deck_heights(s, x, y, layer: int, profile: pd.DataFrame, dem) -> np.ndarray:
    """Top of the deck at chainages `s`, in local meters."""
    from .rail import fit_profile
    ground = dem.sample(x, y) if dem is not None else np.zeros_like(s)
    p = profile.sort_values("s") if len(profile) else profile
    if len(p):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            above = p["z"].values - (dem.sample(*_xy_at(x, y, s, p["s"].values)) if dem is not None else 0)
        p = p[(above > 2.0) & (above < 40.0)]
    if len(p) >= 3:
        return np.interp(s, p["s"].values, fit_profile(p["s"].values, p["z"].values))
    return ground + DEFAULT_RISE * max(layer, 1)


def _xy_at(x, y, s, at):
    return np.interp(at, s, x), np.interp(at, s, y)


def deck_meshes(ways, profiles: pd.DataFrame, dem, bounds, level: int) -> dict[str, mesh.MeshBuilder]:
    """Overpass decks whose segments fall in `bounds` (drawn by the tile holding each midpoint)."""
    from .rail import _beam, _column, _frame
    out = {k: mesh.MeshBuilder() for k in ("deck", "concrete")}
    x0, y0, x1, y1 = bounds
    by_way = dict(tuple(profiles.groupby("way"))) if len(profiles) else {}
    for r in ways.itertuples():
        s, x, y = _samples(r.geometry)
        mx, my = (x[:-1] + x[1:]) / 2, (y[:-1] + y[1:]) / 2
        seg = (mx >= x0) & (mx < x1) & (my >= y0) & (my < y1)
        if not seg.any():
            continue
        z = deck_heights(s, x, y, r.layer, by_way.get(r.osm_id, pd.DataFrame(columns=["s", "z"])), dem)
        idx = np.flatnonzero(seg)
        for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            a, b = run[0], run[-1] + 2
            rx, ry, rz = x[a:b], y[a:b], z[a:b]
            _beam(out["concrete"], rx, ry, rz - 0.05, 0, r.width, SLAB)          # slab
            _beam(out["deck"], rx, ry, rz, 0, r.width - 0.6, 0.05)                 # asphalt top
            if level == 0:
                for side in (-1, 1):
                    _beam(out["concrete"], rx, ry, rz + PARAPET, side * (r.width / 2 - 0.15), 0.3, PARAPET)
        if level == 0:
            cs = np.arange(PIER_SPACING / 2, r.geometry.length, PIER_SPACING)
            if len(cs):
                px, py = np.interp(cs, s, x), np.interp(cs, s, y)
                inside = (px >= x0) & (px < x1) & (py >= y0) & (py < y1)
                top = np.interp(cs, s, z) - SLAB
                ground = dem.sample(px, py) if dem is not None else np.zeros(len(cs))
                for qx, qy, t, g in zip(px[inside], py[inside], top[inside], ground[inside]):
                    if t > g + 2.5:
                        _column(out["concrete"], qx, qy, g, t, size=min(1.4, 0.3 * r.width))
    return out


TRENCH_DROP = 2.5        # m the ground beside a road must stand above it for a retaining wall
TRENCH_HIGHWAYS = ("motorway", "motorway_link", "trunk", "trunk_link")


def trench_walls(feats, dem, bounds, level: int, mb: mesh.MeshBuilder | None = None) -> mesh.MeshBuilder:
    """Vertical retaining walls along expressways in trenches (the Dan Ryan, the Kennedy).

    The ground model is a smoothed 2 m grid, so a sunken expressway's walls came out as
    soft slopes. Along both edges of each expressway that isn't a bridge or tunnel, where
    the ground just beyond the edge stands more than TRENCH_DROP above the road, a wall
    rises from the road to it. Drawn by the tile holding each segment's midpoint.
    """
    from .ground import ROAD_WIDTH, _is_lower_level, _width
    from .rail import _frame
    mb = mb if mb is not None else mesh.MeshBuilder()
    if dem is None or level > 1:
        return mb
    x0, y0, x1, y1 = bounds
    sel = (feats.geometry.geom_type == "LineString") & feats["highway"].isin(TRENCH_HIGHWAYS) \
        & (feats["bridge"].isna() | (feats["bridge"] == "no"))
    for _, r in feats[sel].iterrows():
        if _is_lower_level(r):
            continue
        s, x, y = _samples(r.geometry)
        if len(s) < 2:
            continue
        half = _width(r, ROAD_WIDTH) / 2
        _, nrm = _frame(x, y)
        road = dem.sample(x, y)
        for side in (-1.0, 1.0):
            ex, ey = x + side * nrm[:, 0] * half, y + side * nrm[:, 1] * half
            # The ground model's smoothing spreads a trench edge over a few meters: look 4 and 8 m out.
            beyond = np.maximum(*(dem.sample(x + side * nrm[:, 0] * (half + d), y + side * nrm[:, 1] * (half + d))
                                  for d in (4.0, 8.0)))
            tall = (beyond - road) > TRENCH_DROP
            mx, my = (ex[:-1] + ex[1:]) / 2, (ey[:-1] + ey[1:]) / 2
            seg = tall[:-1] & tall[1:] & (mx >= x0) & (mx < x1) & (my >= y0) & (my < y1)
            for k in np.flatnonzero(seg):
                quad = np.array([[ex[k], ey[k], road[k]], [ex[k + 1], ey[k + 1], road[k + 1]],
                                 [ex[k + 1], ey[k + 1], beyond[k + 1]], [ex[k], ey[k], beyond[k]]])
                n = np.array([-side * nrm[k, 0], -side * nrm[k, 1], 0.0])   # facing the road
                tri = np.array([0, 1, 2, 0, 2, 3], np.uint32) if side < 0 else np.array([0, 2, 1, 0, 3, 2], np.uint32)
                mb.add(quad, np.tile(n, (4, 1)), tri)
    return mb
