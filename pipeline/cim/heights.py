"""Building heights from OSM tags and city attributes, with the source recorded."""
import math
import re

from .config import load_config

FT = 0.3048
_NUM = r"(\d+(?:\.\d+)?)"


def parse_length(value) -> float | None:
    """Parse an OSM length such as '120', '120 m', '394 ft', "394'", or "12'6\"". Returns meters."""
    if value is None:
        return None
    s = str(value).strip().lower().replace(",", ".")
    if not s:
        return None
    if m := re.fullmatch(_NUM + r"\s*(m|meters?|metres?)?", s):
        return float(m.group(1))
    if m := re.fullmatch(_NUM + r"\s*(ft|feet|')", s):
        return float(m.group(1)) * FT
    if m := re.fullmatch(r"(\d+)\s*'\s*(\d+(?:\.\d+)?)\s*(\"|in)?", s):
        return int(m.group(1)) * FT + float(m.group(2)) * 0.0254
    return None


def parse_number(value) -> float | None:
    if value is None:
        return None
    try:
        n = float(str(value).strip().split(";")[0])
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def resolve(tags: dict, city_stories=None, footprint_m2: float | None = None) -> tuple[float, float, str]:
    """Return (height, min_height, source) for one building or building part.

    Order: OSM height → OSM building:levels → City of Chicago stories → default by type.
    """
    cfg = load_config()["heights"]
    per_level = cfg["meters_per_level"]

    min_h = parse_length(tags.get("min_height"))
    if min_h is None and (lv := parse_number(tags.get("building:min_level"))) is not None:
        min_h = lv * per_level
    min_h = max(min_h or 0.0, 0.0)

    h = parse_length(tags.get("height"))
    if h is not None and h > 0:
        return max(h, min_h + 0.5), min_h, "osm:height"

    levels = parse_number(tags.get("building:levels"))
    if levels is not None and levels > 0:
        roof = parse_number(tags.get("roof:levels")) or 0
        return max((levels + roof) * per_level, min_h + 0.5), min_h, "osm:levels"

    stories = parse_number(city_stories)
    if stories is not None and stories > 0:
        return max(stories * cfg["meters_per_story"], min_h + 0.5), min_h, "city:stories"

    kind = tags.get("building") or tags.get("building:part") or "yes"
    h = cfg["defaults"].get(kind, cfg["default"])
    if footprint_m2 is not None and footprint_m2 < cfg["small_footprint_m2"]:
        h = min(h, cfg["small_footprint_height"])  # kiosks, booths, canopies
    return max(h, min_h + 0.5), min_h, "default"
