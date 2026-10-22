"""Railways: the L's elevated structure, rails and platforms, and bridge decks for all rail.

Track alignment comes from OpenStreetMap: CTA lines are `railway=subway` (and a
little `light_rail`), one way per track, with `bridge`/`layer` where elevated.
Metra and freight lines are `railway=rail`.

Deck heights come from the lidar: `python -m cim rail` samples the top surface
along every elevated track every SAMPLE m and stores the profile per 2 km block in
data/work/rail/<i>_<j>.parquet (way, chainage s along the whole way, z). Tiles turn
the alignment and profiles into meshes.
"""
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, box

from . import mesh
from .config import lidar_work, lidar_workers, load_config
from .grid import Key
from .lidar import GROUND, HIGH_NOISE, NOISE, WATER, Dem, block_paths, load_dem, read_points
from .osm import load_features

_FRESH = multiprocessing.get_context("forkserver")

RAIL = lidar_work("rail")
SAMPLE = 4.0          # meters between profile samples and mesh segments
GAUGE = 1.435         # standard gauge
COLUMN_SPACING = 13.0 # L columns
COLUMN_OFFSET = 2.0   # m outward from a paired track's centerline
DEFAULT_DECK = 7.5    # m above ground when the lidar has nothing
L_TYPES = ("subway", "light_rail")
RAIL_TYPES = ("rail", "subway", "light_rail")


def _layer(v) -> int:
    try:
        return int(str(v).split(";")[0])
    except (TypeError, ValueError):
        return 0


def railways(feats) -> pd.DataFrame:
    """Rail lines that are above ground, with `is_l` and `elevated` flags."""
    r = feats[feats["railway"].isin(RAIL_TYPES) & (feats.geometry.geom_type == "LineString")].copy()
    tunnel = r["tunnel"].isin(["yes", "culvert"]) | (r["layer"].map(_layer) < 0) | (r["covered"] == "yes")
    r = r[~tunnel]
    r["is_l"] = r["railway"].isin(L_TYPES)
    r["elevated"] = r["bridge"].isin(["yes", "viaduct", "movable", "trestle"]) | (r["layer"].map(_layer) > 0)
    return r


# ---------------------------------------------------------------- lidar profiles

def _dsm(x, y, z, cls_, dem: Dem, bounds, cell: float = 1.0):
    """Highest non-ground point per 1 m cell, ignoring anything more than 20 m above the ground."""
    x0, y0, x1, y1 = bounds
    w, h = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
    m = (cls_ != GROUND) & (cls_ != NOISE) & (cls_ != HIGH_NOISE) & (cls_ != WATER)
    x, y, z = x[m], y[m], z[m]
    above = z - dem.sample(x, y)
    keep = (above > 2.0) & (above < 20.0)
    x, y, z = x[keep], y[keep], z[keep]
    j = np.clip(((x - x0) / cell).astype(int), 0, w - 1)
    i = np.clip(((y - y0) / cell).astype(int), 0, h - 1)
    grid = np.full(w * h, np.nan, np.float32)
    np.fmax.at(grid, i * w + j, z.astype(np.float32))
    return grid.reshape(h, w), x0, y0, cell


def _samples(line: LineString):
    """(chainage, x, y) every SAMPLE m along a line, chainage from the line's start."""
    n = max(2, int(np.ceil(line.length / SAMPLE)) + 1)
    s = np.linspace(0, line.length, n)
    pts = shapely.line_interpolate_point(line, s)
    return s, shapely.get_x(pts), shapely.get_y(pts)


def profile_block(key: Key) -> pd.DataFrame:
    x0, y0, x1, y1 = key.bounds
    grown = (x0 - 20, y0 - 20, x1 + 20, y1 + 20)
    feats = load_features(grown)
    rails = railways(feats)
    rails = rails[rails["elevated"] & rails.intersects(box(*grown))]
    if not len(rails):
        return pd.DataFrame(columns=["way", "s", "z"])
    dem = load_dem(key)
    x, y, z, c = read_points(grown, load_config()["lidar"]["depth"])
    grid, gx, gy, cell = _dsm(x, y, z, c, dem, grown)
    # Station canopies read 3-5 m above the deck (Washington/Wells), so samples near
    # platforms and station roofs are dropped; the fit interpolates across them.
    stations = feats[(feats["railway"] == "platform") | feats["building"].isin(["train_station", "roof"])]
    covered = shapely.union_all(stations.geometry.buffer(3.0).values) if len(stations) else None
    if covered is not None:
        shapely.prepare(covered)
    rows = []
    for way, line in zip(rails["osm_id"], rails.geometry):
        s, sx, sy = _samples(line)
        inside = (sx >= x0) & (sx < x1) & (sy >= y0) & (sy < y1)
        if not inside.any():
            continue
        s, sx, sy = s[inside], sx[inside], sy[inside]
        j = ((sx - gx) / cell).astype(int)
        i = ((sy - gy) / cell).astype(int)
        vals = np.stack([grid[np.clip(i + di, 0, grid.shape[0] - 1), np.clip(j + dj, 0, grid.shape[1] - 1)]
                         for di in (-1, 0, 1) for dj in (-1, 0, 1)])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # samples with no points at all
            zs = np.nanmedian(vals, axis=0)
        ok = np.isfinite(zs)
        if covered is not None:
            ok &= ~shapely.contains_xy(covered, sx, sy)
        rows.append(pd.DataFrame({"way": way, "s": s[ok], "z": zs[ok]}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["way", "s", "z"])


SETTINGS = "v2 no-station-canopies"  # cached blocks made with other settings are redone


def _stamp(key: Key):
    return RAIL / f"{key.i}_{key.j}.settings"


def _profile_job(key: Key):
    t0 = time.time()
    df = profile_block(key)
    df.to_parquet(RAIL / f"{key.i}_{key.j}.parquet")
    _stamp(key).write_text(SETTINGS)
    return key, len(df), time.time() - t0


def run(blocks, force: bool = False) -> None:
    RAIL.mkdir(parents=True, exist_ok=True)

    def current(k):
        return (RAIL / f"{k.i}_{k.j}.parquet").exists() and _stamp(k).exists() and _stamp(k).read_text() == SETTINGS
    todo = [k for k in sorted(blocks) if (force or not current(k)) and block_paths(k)[0].exists()]
    if not todo:
        print("  rail: all blocks up to date")
        return
    workers = lidar_workers(load_config()["lidar"]["workers"])
    print(f"  rail: profiling elevated track in {len(todo)} blocks, {workers} workers")
    t0 = time.time()
    with ProcessPoolExecutor(mp_context=_FRESH, max_workers=workers) as pool:
        for n, (key, nrows, secs) in enumerate(pool.map(_profile_job, todo), 1):
            if n % 20 == 0 or n == len(todo):
                print(f"    {n}/{len(todo)} blocks, {time.time() - t0:.0f}s")


def load_profiles(keys) -> pd.DataFrame:
    parts = [pd.read_parquet(RAIL / f"{k.i}_{k.j}.parquet") for k in keys if (RAIL / f"{k.i}_{k.j}.parquet").exists()]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["way", "s", "z"])


# ---------------------------------------------------------------- deck heights

def deck_heights(line: LineString, s: np.ndarray, x, y, elevated: bool, profile: pd.DataFrame, dem) -> np.ndarray:
    """Top of the deck (rail base) at chainages `s`, in local meters."""
    ground = dem.sample(x, y) if dem is not None else np.zeros_like(s)
    if not elevated:
        return ground + 0.3
    p = profile.sort_values("s") if len(profile) else profile
    if len(p):
        above = p["z"].values - (dem.sample(*_xy(line, p["s"].values)) if dem is not None else 0)
        good = (above > 3.0) & (above < 16.0)
        p = p[good]
    if len(p) >= 3:
        z = fit_profile(p["s"].values, p["z"].values)
        # The lidar sees the top of the rails and ties; the deck top is a little lower.
        return np.interp(s, p["s"].values, z) - 0.2
    return ground + DEFAULT_DECK


MAX_GRADE = 0.05   # rail is rarely steeper than 4%; a 5% cap removes spikes, not real ramps
OUTLIER_M = 0.75


def fit_profile(s: np.ndarray, z: np.ndarray, window: int = 9) -> np.ndarray:
    """Deck profile from top-surface samples: robust to trains, trusses and gantries.

    The visual audit found kinks of 58% and 132% grade (Wells Street bridge, Metra at
    55th Street) from outliers a 5-sample median let through. A wide median, then
    samples within OUTLIER_M of it, then a grade limit swept both ways.
    """
    med = pd.Series(z).rolling(window, center=True, min_periods=1).median().values
    good = np.abs(z - med) <= OUTLIER_M
    if good.sum() >= 3:
        med = np.interp(s, s[good], pd.Series(z[good]).rolling(window, center=True, min_periods=1).median().values)
    ds = np.diff(s)
    fwd, back = med.copy(), med.copy()
    for k in range(1, len(s)):
        fwd[k] = np.clip(fwd[k], fwd[k - 1] - MAX_GRADE * ds[k - 1], fwd[k - 1] + MAX_GRADE * ds[k - 1])
    for k in range(len(s) - 2, -1, -1):
        back[k] = np.clip(back[k], back[k + 1] - MAX_GRADE * ds[k], back[k + 1] + MAX_GRADE * ds[k])
    return (fwd + back) / 2


def _xy(line, s):
    pts = shapely.line_interpolate_point(line, s)
    return shapely.get_x(pts), shapely.get_y(pts)


# ---------------------------------------------------------------- meshes

def _frame(x, y):
    """Unit tangent and left normal at each sample of a polyline."""
    d = np.zeros((len(x), 2))
    d[1:-1] = np.c_[x[2:] - x[:-2], y[2:] - y[:-2]]
    d[0] = [x[1] - x[0], y[1] - y[0]]
    d[-1] = [x[-1] - x[-2], y[-1] - y[-2]]
    d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    return d, np.c_[-d[:, 1], d[:, 0]]


def _thin(x, y, z, tol: float = 0.08) -> np.ndarray:
    """Indices of samples to keep: drop those a straight segment between neighbors
    reproduces within `tol` m. Straight, level track keeps only its ends."""
    n = len(x)
    if n <= 2:
        return np.arange(n)
    pts = np.c_[x, y, z]
    keep = [0]
    i = 0
    while i < n - 1:
        j = i + 2
        # Extend the segment from i while every sample in between stays within tol.
        while j < n:
            seg = pts[j] - pts[i]
            t = np.clip(((pts[i + 1:j] - pts[i]) @ seg) / max(seg @ seg, 1e-9), 0, 1)
            err = np.linalg.norm(pts[i] + t[:, None] * seg - pts[i + 1:j], axis=1).max()
            if err > tol or (j - i) * SAMPLE > 60:
                break
            j += 1
        keep.append(j - 1)
        i = j - 1
    return np.asarray(keep)


def _beam(mb: mesh.MeshBuilder, x, y, z_top, offset: float, width: float, depth: float) -> None:
    """A box section swept along the polyline: `offset` m left of the centerline."""
    _, nrm = _frame(x, y)
    cx, cy = x + nrm[:, 0] * offset, y + nrm[:, 1] * offset
    hw = width / 2
    left = np.c_[cx + nrm[:, 0] * hw, cy + nrm[:, 1] * hw]
    right = np.c_[cx - nrm[:, 0] * hw, cy - nrm[:, 1] * hw]
    top, bot = z_top, z_top - depth
    # Four long faces: top, bottom, left side, right side.
    faces = [
        (left, top, right, top, (0, 0, 1)),
        (right, bot, left, bot, (0, 0, -1)),
        (left, bot, left, top, None),     # left wall
        (right, top, right, bot, None),   # right wall
    ]
    n = len(x)
    for a, za, b, zb, normal in faces:
        pos = np.empty(((n - 1) * 4, 3))
        pos[0::4, :2], pos[0::4, 2] = a[:-1], za[:-1]
        pos[1::4, :2], pos[1::4, 2] = a[1:], za[1:]
        pos[2::4, :2], pos[2::4, 2] = b[1:], zb[1:]
        pos[3::4, :2], pos[3::4, 2] = b[:-1], zb[:-1]
        if normal is None:
            side = 1.0 if a is left else -1.0
            nn = np.repeat(np.c_[nrm[:-1] * side, np.zeros(n - 1)], 4, axis=0)
        else:
            nn = np.tile(normal, ((n - 1) * 4, 1)).astype(float)
        base = np.arange(n - 1, dtype=np.uint32) * 4
        mb.add(pos, nn, np.column_stack([base, base + 1, base + 2, base, base + 2, base + 3]).reshape(-1))


def _column(mb: mesh.MeshBuilder, x: float, y: float, z0: float, z1: float, size: float = 0.6) -> None:
    mesh.add_extrusion(mb, box(x - size / 2, y - size / 2, x + size / 2, y + size / 2), z0, z1, 0)


def _outer_sides(tracks) -> dict[str, float]:
    """+1 or -1: which side of each elevated L track faces away from its paired track."""
    el = tracks[tracks["elevated"] & tracks["is_l"]]
    if len(el) < 2:
        return {}
    geoms = el.geometry.values
    tree = shapely.STRtree(geoms)
    out = {}
    for k, (way, line) in enumerate(zip(el["osm_id"], geoms)):
        mid = line.interpolate(0.5, normalized=True)
        cand = [c for c in tree.query(mid.buffer(8)) if c != k]
        if not cand:
            continue
        other = min(cand, key=lambda c: geoms[c].distance(mid))
        p = geoms[other].interpolate(geoms[other].project(mid))
        s0 = line.project(mid)
        a, b = line.interpolate(max(s0 - 1, 0)), line.interpolate(min(s0 + 1, line.length))
        cross = (b.x - a.x) * (p.y - mid.y) - (b.y - a.y) * (p.x - mid.x)
        out[way] = -1.0 if cross > 0 else 1.0  # the neighbor is on the left: columns go right
    return out


def track_meshes(tracks, profiles: pd.DataFrame, dem, bounds, level: int) -> dict[str, mesh.MeshBuilder]:
    """Meshes for the tracks whose segments fall in `bounds` at tile `level`.

    Each segment is drawn by the tile containing its midpoint, and columns sit at
    fixed chainages from each way's start, so neighboring tiles join seamlessly.
    """
    out = {k: mesh.MeshBuilder() for k in ("steel", "rails", "ballast")}
    x0, y0, x1, y1 = bounds
    by_way = dict(tuple(profiles.groupby("way"))) if len(profiles) else {}
    sides = _outer_sides(tracks)
    for t in tracks.itertuples():
        line = t.geometry
        s, x, y = _samples(line)
        mx, my = (x[:-1] + x[1:]) / 2, (y[:-1] + y[1:]) / 2
        seg = (mx >= x0) & (mx < x1) & (my >= y0) & (my < y1)
        if not seg.any():
            continue
        z = deck_heights(line, s, x, y, t.elevated, by_way.get(t.osm_id, pd.DataFrame(columns=["s", "z"])), dem)
        # Contiguous runs of segments inside the tile.
        idx = np.flatnonzero(seg)
        runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        for run in runs:
            a, b = run[0], run[-1] + 2  # sample indices a..b-1
            keep = _thin(x[a:b], y[a:b], z[a:b])
            rx, ry, rz = x[a:b][keep], y[a:b][keep], z[a:b][keep]
            if t.elevated:
                _beam(out["steel"], rx, ry, rz, 0, 3.4, 0.5)                    # deck
                if level == 0:
                    for off in (-1.1, 1.1):
                        _beam(out["steel"], rx, ry, rz - 0.5, off, 0.4, 1.0)    # girders
            elif level == 0:
                _beam(out["ballast"], rx, ry, rz, 0, 3.2, 0.5)                   # railbed
            if level == 0 and t.is_l:
                for off in (-GAUGE / 2, GAUGE / 2):
                    _beam(out["rails"], rx, ry, rz + 0.17, off, 0.08, 0.17)
        if t.elevated and t.is_l and level <= 1:
            spacing = COLUMN_SPACING * (1 if level == 0 else 2)
            cs = np.arange(spacing / 2, line.length, spacing)
            cx, cy = _xy(line, cs)
            # Columns stand outside the pair of tracks (near the curbs), not under each track.
            side = sides.get(t.osm_id, 0.0)
            if side:
                d, nrm = _frame(cx, cy) if len(cx) > 1 else (None, np.array([[0.0, 0.0]]))
                cx, cy = cx + nrm[:, 0] * side * COLUMN_OFFSET, cy + nrm[:, 1] * side * COLUMN_OFFSET
            inside = (cx >= x0) & (cx < x1) & (cy >= y0) & (cy < y1)
            if inside.any():
                cz = np.interp(cs[inside], s, z) - 1.5
                g = dem.sample(cx[inside], cy[inside]) if dem is not None else np.zeros(inside.sum())
                for px, py, top, bottom in zip(cx[inside], cy[inside], cz, g):
                    if top > bottom + 1.0:
                        _column(out["steel"], px, py, bottom, top)
    return out


def platform_meshes(platforms, tracks, profiles: pd.DataFrame, dem, bounds) -> dict[str, mesh.MeshBuilder]:
    """Platforms beside L tracks, at deck height plus 1.1 m, with a canopy."""
    out = {"platform": mesh.MeshBuilder(), "canopy": mesh.MeshBuilder()}
    l_tracks = tracks[tracks["is_l"]]
    if not len(platforms) or not len(l_tracks):
        return out
    by_way = dict(tuple(profiles.groupby("way"))) if len(profiles) else {}
    x0, y0, x1, y1 = bounds
    tree = shapely.STRtree(l_tracks.geometry.values)
    for poly in platforms.geometry:
        c = poly.representative_point()
        if not (x0 <= c.x < x1 and y0 <= c.y < y1):
            continue
        near = tree.query_nearest(c, max_distance=12)
        if not len(near):
            continue
        t = l_tracks.iloc[int(near[0])]
        s = np.array([t.geometry.project(c)])
        px, py = _xy(t.geometry, s)
        z = float(deck_heights(t.geometry, s, px, py, t.elevated,
                               by_way.get(t.osm_id, pd.DataFrame(columns=["s", "z"])), dem)[0])
        top = z + 0.17 + 1.1
        for part in getattr(poly, "geoms", [poly]):
            if part.geom_type != "Polygon":
                continue
            bottom = top - 0.4 if t.elevated else (float(dem.sample(c.x, c.y)) if dem is not None else 0.0)
            mesh.add_extrusion(out["platform"], part, bottom, top, 0)
            mesh.add_extrusion(out["canopy"], part.buffer(0.3, join_style="mitre"), top + 3.2, top + 3.4, 0)
    return out


def platforms_from(feats):
    """Platform areas (lines are buffered to 3 m wide)."""
    p = feats[feats["railway"] == "platform"]
    polys = p[p.geometry.geom_type.isin(("Polygon", "MultiPolygon"))].geometry
    lines = p[p.geometry.geom_type == "LineString"].geometry.buffer(1.5, cap_style="flat")
    return pd.DataFrame({"geometry": list(polys) + list(lines)})
