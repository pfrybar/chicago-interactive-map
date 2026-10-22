"""Checks against Cook County's 2025 orthophotos (red, green, blue, near-infrared).

    python -m cim ortho --area 32

The near-infrared band separates living vegetation from everything else (NDVI). Two
checks per community area, written to data/work/ortho/area-<n>.json:

- trees_without_canopy: trees we place (from the lidar) where the 2025 photo shows no
  vegetation under the crown: removed since the survey, or never a tree.
- footprints_on_vegetation: buildings whose footprint is mostly vegetation in 2025:
  demolished, mapped in the wrong place, or a green roof.

The 2025 flights ran from March to May, so some 500 m tiles are leaf-off. A tile where
most trees show no canopy is reported as leaf-off and its trees aren't flagged.

The imagery's terms forbid redistributing it or making it available over a network
without the county's permission (see sources.yaml). It's cached under data/raw/ and
review crops under data/work/ortho/review/; neither is ever committed or published.
Fixes based on it are tagged `evidence: cook_ortho_2025`.
"""
import io
import json
import time
from functools import lru_cache

import geopandas as gpd
import numpy as np
import requests
import shapely
from PIL import Image, ImageDraw
from pyproj import Transformer

from .config import RAW, WORK, load_sources
from .frame import local_frame
from .grid import Key

ORTHO_RAW = RAW / "cook_ortho_2025"
OUT = WORK / "ortho"
STATE_PLANE = "EPSG:6455"
PIXEL = 0.3            # meters per pixel requested (the source is 0.15)
# The near-infrared band isn't calibrated against the others: in shadow, almost every
# pixel has NDVI > 0.25. So vegetation needs a high NDVI *and* a bright near-infrared
# value, and dark pixels (tower shadows downtown) count as unknown. Calibrated on Grant
# Park and the Loop core: roofs read 1-5% vegetation, lawns and tree crowns 35-60%.
VEG_NDVI = 0.3
VEG_NIR = 170
DARK = 70              # mean of red, green and blue below this is "unknown"
LIT_SAMPLES = 5        # of the 9 samples under a crown, at least this many must be lit
LEAF_OFF_GRASS = 0.1   # a tile is leaf-off when its lit lawns and parks are less green than this


@lru_cache
def _to_sp() -> Transformer:
    return Transformer.from_crs(local_frame().crs, STATE_PLANE, always_xy=True)


class Ortho:
    """One leaf tile's image: bands (h, w, 4) in state plane, with its state-plane bounds."""

    def __init__(self, bands: np.ndarray, bounds_sp):
        self.bands, self.bounds_sp = bands, bounds_sp
        nir, red = bands[..., 3].astype(np.float32), bands[..., 0].astype(np.float32)
        self.ndvi = (nir - red) / np.maximum(nir + red, 1.0)
        self.dark = bands[..., :3].mean(axis=-1) < DARK
        self.veg = (self.ndvi > VEG_NDVI) & (bands[..., 3] >= VEG_NIR) & ~self.dark

    def pixel(self, x, y):
        """Row, column of local (x, y) points (clipped to the image)."""
        sx, sy = _to_sp().transform(np.asarray(x, float), np.asarray(y, float))
        x0, y0, x1, y1 = self.bounds_sp
        h, w = self.ndvi.shape
        col = np.clip(((sx - x0) / (x1 - x0) * w).astype(int), 0, w - 1)
        row = np.clip(((y1 - sy) / (y1 - y0) * h).astype(int), 0, h - 1)
        return row, col


def fetch(leaf: Key) -> Ortho:
    """The 2025 photo of one 500 m leaf tile, cached as compressed numpy."""
    path = ORTHO_RAW / f"{leaf.i}_{leaf.j}.npz"
    if path.exists():
        d = np.load(path)
        return Ortho(d["bands"], tuple(d["bounds_sp"]))
    x0, y0, x1, y1 = leaf.bounds
    xs, ys = _to_sp().transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    bounds = (min(xs), min(ys), max(xs), max(ys))
    w = h = int(round((x1 - x0) / PIXEL))
    url = load_sources()["cook_ortho_2025"]["url"] + "/exportImage"
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=180, params={
                "bbox": ",".join(map(str, bounds)), "bboxSR": 6455, "imageSR": 6455,
                "size": f"{w},{h}", "format": "tiff", "pixelType": "U8", "f": "image"})
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(5 * 2 ** attempt)
    bands = np.asarray(Image.open(io.BytesIO(r.content)))
    if bands.ndim != 3 or bands.shape[2] < 4:
        raise RuntimeError(f"unexpected ortho response for {leaf}: {bands.shape}")
    ORTHO_RAW.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, bands=bands[..., :4], bounds_sp=np.array(bounds))
    return Ortho(bands[..., :4], bounds)


def _disk_values(o: Ortho, x, y, r) -> tuple[np.ndarray, np.ndarray]:
    """(vegetation, dark) sampled on a small pattern within radius r of each point, each (n, 9)."""
    angles = np.linspace(0, 2 * np.pi, 8, endpoint=False)
    dx = np.concatenate([[0], 0.6 * np.cos(angles)])
    dy = np.concatenate([[0], 0.6 * np.sin(angles)])
    px = x[:, None] + r[:, None] * dx[None, :]
    py = y[:, None] + r[:, None] * dy[None, :]
    row, col = o.pixel(px.ravel(), py.ravel())
    return o.veg[row, col].reshape(px.shape), o.dark[row, col].reshape(px.shape)


def trees_without_canopy(o: Ortho, trees) -> np.ndarray:
    """Trees whose crown is lit in the photo but shows (almost) no vegetation."""
    if not len(trees):
        return np.zeros(0, bool)
    veg, dark = _disk_values(o, trees["x"].values, trees["y"].values, trees["radius"].values)
    lit = (~dark).sum(axis=1)
    green = veg.sum(axis=1) / np.maximum(lit, 1)
    return (lit >= LIT_SAMPLES) & (green < 0.15)


def grass_greenness(o: Ortho, leaf: Key) -> float | None:
    """Vegetation share of the lit pixels in a tile's mapped lawns and parks (None if it has none)."""
    from .osm import load_features
    f = load_features(leaf.bounds)
    sel = ((f["leisure"] == "park") | (f["landuse"] == "grass")) & f.geometry.geom_type.isin(("Polygon", "MultiPolygon"))
    if not sel.any():
        return None
    x0, y0, x1, y1 = leaf.bounds
    gx, gy = np.meshgrid(np.arange(x0, x1, 3.0), np.arange(y0, y1, 3.0))
    gx, gy = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(shapely.union_all([shapely.make_valid(g) for g in f[sel].geometry.values]), gx, gy)
    if inside.sum() < 200:
        return None
    row, col = o.pixel(gx[inside], gy[inside])
    lit = ~o.dark[row, col]
    return float(o.veg[row, col][lit].mean()) if lit.sum() >= 100 else None


def vegetation_share(o: Ortho, polygon) -> float:
    """Share of a footprint's lit interior (1 m inside its edge) that is vegetation (0 if mostly dark)."""
    inner = polygon.buffer(-1.0)
    if inner.is_empty or inner.area < 4:
        return 0.0
    x0, y0, x1, y1 = inner.bounds
    step = max(PIXEL * 2, np.sqrt(inner.area / 400))
    gx, gy = np.meshgrid(np.arange(x0, x1, step), np.arange(y0, y1, step))
    gx, gy = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(inner, gx, gy)
    if not inside.any():
        return 0.0
    row, col = o.pixel(gx[inside], gy[inside])
    lit = ~o.dark[row, col]
    if lit.mean() < 0.5:
        return 0.0
    return float(o.veg[row, col][lit].mean())


def _crop(o: Ortho, x, y, half: float, shapes, path) -> None:
    """A local review image around (x, y) with our shapes outlined (never committed)."""
    row, col = o.pixel(np.array([x - half, x + half]), np.array([y + half, y - half]))
    r0, r1, c0, c1 = min(row), max(row) + 1, min(col), max(col) + 1
    img = Image.fromarray(o.bands[r0:r1, c0:c1, :3]).convert("RGB")
    draw = ImageDraw.Draw(img)
    for g in shapes:
        for ring in getattr(g, "geoms", [g]):
            ring = getattr(ring, "exterior", ring)
            xs, ys = np.asarray(ring.coords).T
            rr, cc = o.pixel(xs, ys)
            draw.line(list(zip((cc - c0).tolist(), (rr - r0).tolist())), fill=(255, 40, 40), width=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.resize((img.width * 2, img.height * 2)).save(path, quality=85)


def check_area(area: int, review: bool = True) -> dict:
    """Run both checks for one community area; returns and writes the report."""
    from .areas import area_polygon_local
    from .grid import index as grid_index, BLOCK_LEVEL
    from .tiles import build_leaves
    from . import trees as trees_mod

    boundary = area_polygon_local(area)
    leaves = [k for k in build_leaves([area]) if k.polygon().intersects(boundary)]
    buildings = gpd.read_parquet(WORK / f"area-{area}" / "buildings.parquet")
    centers = buildings.geometry.representative_point()
    li, lj = grid_index(centers.x.values, centers.y.values, 0)
    review_dir = OUT / "review" / f"area-{area}"

    leaf_off, trees_flagged, fp_flagged, checked_trees = [], [], [], 0
    block_trees: dict = {}
    for n, leaf in enumerate(sorted(leaves), 1):
        o = fetch(leaf)
        block = leaf.ancestor(BLOCK_LEVEL)
        if block not in block_trees:
            block_trees[block] = trees_mod.load(block)
        t = block_trees[block]
        if t is not None and len(t):
            x0, y0, x1, y1 = leaf.bounds
            t = t[(t.x >= x0) & (t.x < x1) & (t.y >= y0) & (t.y < y1)]
            t = t[shapely.contains_xy(boundary, t.x.values, t.y.values)]
            checked_trees += len(t)
            green = grass_greenness(o, leaf)
            if green is not None and green < LEAF_OFF_GRASS:
                leaf_off.append({"leaf": [leaf.i, leaf.j], "trees": int(len(t)), "lawn_greenness": round(green, 2)})
            else:
                bare = trees_without_canopy(o, t)
                for r in t[bare].itertuples():
                    trees_flagged.append({"at": [round(r.x, 1), round(r.y, 1)], "height": round(float(r.height), 1),
                                          "radius": round(float(r.radius), 1), "leaf": [leaf.i, leaf.j]})
        sel = buildings[(li == leaf.i) & (lj == leaf.j)]
        for idx, b in sel.iterrows():
            if b.geometry.area < 40:
                continue
            share = vegetation_share(o, b.geometry)
            if share >= 0.6:
                c = b.geometry.representative_point()
                item = {"id": b["id"], "name": b["name"] if isinstance(b["name"], str) else None,
                        "kind": b["kind"], "vegetation": round(share, 2), "area_m2": round(b.geometry.area),
                        "at": [round(c.x), round(c.y)]}
                fp_flagged.append(item)
                if review:
                    _crop(o, c.x, c.y, max(25.0, np.sqrt(b.geometry.area)), [b.geometry],
                          review_dir / f"fp_{b['id'].replace(':', '_')}.jpg")
        if n % 10 == 0 or n == len(leaves):
            print(f"    {n}/{len(leaves)} tiles, {len(trees_flagged)} trees and {len(fp_flagged)} footprints flagged",
                  flush=True)
    if review:
        for k, item in enumerate(sorted(trees_flagged, key=lambda d: -d["height"])[:40]):
            o = fetch(Key(0, *item["leaf"]))
            x, y = item["at"]
            _crop(o, x, y, 20.0, [shapely.Point(x, y).buffer(item["radius"])], review_dir / f"tree_{k:02d}.jpg")
    report = {
        "area": area, "imagery": "cook_ortho_2025", "trees_checked": checked_trees,
        "leaf_off_tiles": leaf_off,
        "trees_without_canopy": sorted(trees_flagged, key=lambda d: -d["height"]),
        "footprints_on_vegetation": sorted(fp_flagged, key=lambda d: -d["area_m2"]),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"area-{area}.json").write_text(json.dumps(report, indent=1))
    print(f"  ortho area {area}: {checked_trees:,} trees checked, {len(trees_flagged)} without canopy, "
          f"{len(leaf_off)} leaf-off tiles, {len(fp_flagged)} footprints on vegetation")
    return report
