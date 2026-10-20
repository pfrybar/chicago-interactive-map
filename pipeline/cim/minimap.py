"""The minimap: one top-down image of the whole build, plus area outlines."""
import numpy as np
import shapely
from PIL import Image, ImageDraw

from .areas import area_polygon_local, area_row

METERS_PER_PIXEL = 8.0
COLORS = {
    "land": (217, 214, 207), "green": (169, 191, 150), "water": (127, 167, 189),
    "road": (244, 242, 238), "building": (168, 162, 151),
}


def render(out_dir, areas, buildings, parts: list[dict], extent) -> dict:
    """Write minimap.webp and return the overview metadata the viewer reads."""
    x0, y0, x1, y1 = extent.bounds
    w = int(np.ceil((x1 - x0) / METERS_PER_PIXEL))
    h = int(np.ceil((y1 - y0) / METERS_PER_PIXEL))
    img = Image.new("RGB", (w, h), (205, 201, 193))
    draw = ImageDraw.Draw(img)

    def px(coords):
        c = np.asarray(coords)
        return list(zip((c[:, 0] - x0) / METERS_PER_PIXEL, (y1 - c[:, 1]) / METERS_PER_PIXEL))

    def fill(geom, color):
        for poly in getattr(geom, "geoms", [geom]):
            if poly.geom_type != "Polygon" or poly.is_empty:
                continue
            draw.polygon(px(poly.exterior.coords), fill=color)
            for hole in poly.interiors:
                draw.polygon(px(hole.coords), fill=COLORS["land"])

    fill(extent, COLORS["land"])
    for layer in ("green", "water", "road"):
        for part in parts:
            if layer in part:
                fill(shapely.from_wkb(part[layer]), COLORS[layer])
    for geom in buildings.geometry.simplify(2.0):
        fill(geom, COLORS["building"])
    img.save(out_dir / "minimap.webp", lossless=True, quality=100, method=4)

    return {
        "bounds": [round(v, 1) for v in (x0, y0, x1, y1)],
        "image": "minimap.webp",
        "metersPerPixel": METERS_PER_PIXEL,
        "size": [w, h],
        "areas": [_area(a) for a in areas],
    }


def _area(number: int) -> dict:
    poly = area_polygon_local(number)
    c = poly.representative_point()
    rings = [[[round(x, 1), round(y, 1)] for x, y in p.exterior.coords]
             for p in getattr(poly.simplify(10.0), "geoms", [poly.simplify(10.0)])]
    return {"number": number, "name": area_row(number)["name"], "center": [round(c.x, 1), round(c.y, 1)],
            "rings": rings}
