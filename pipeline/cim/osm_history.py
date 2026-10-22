"""When an OSM building got its current height tags, from the OSM API's version history.

Used only for the few buildings whose mapped height the lidar contradicts badly
(newer_than_lidar in tiles.py). A height or floor count that was already on the map
before the survey flew isn't evidence of a building finished after it: it's just a
wrong tag (Sheridan Glen, 21 floors since 2013, is a low-rise).

Responses are cached in data/raw/osm_history/. Requests are spaced a second apart and
identify the project, per the OSM API usage policy.
"""
import json
import time
from datetime import datetime

import requests

from .config import RAW

CACHE = RAW / "osm_history"
API = "https://api.openstreetmap.org/api/0.6"
HEIGHT_TAGS = ("height", "building:levels")
_last = [0.0]


def _history(osm_id: str) -> list[dict] | None:
    kind = {"w": "way", "r": "relation"}.get(osm_id[:1])
    if kind is None:
        return None
    path = CACHE / f"{osm_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    wait = 1.0 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    try:
        r = requests.get(f"{API}/{kind}/{osm_id[1:]}/history.json", timeout=60,
                         headers={"User-Agent": "chicago-interactive-map (github.com/pfrybar/chicago-interactive-map)"})
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  osm history: {osm_id}: {e}")
        return None
    versions = [{"version": e["version"], "timestamp": e["timestamp"], "tags": e.get("tags", {})}
                for e in r.json().get("elements", [])]
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(versions))
    return versions


def height_tags_since(osm_id: str) -> datetime | None:
    """When the current height/levels tag values first appeared, unchanged since (None if unknown)."""
    versions = _history(osm_id)
    if not versions:
        return None
    versions = sorted(versions, key=lambda v: v["version"])
    current = tuple(versions[-1]["tags"].get(k) for k in HEIGHT_TAGS)
    since = versions[-1]["timestamp"]
    for v in reversed(versions):
        if tuple(v["tags"].get(k) for k in HEIGHT_TAGS) != current:
            break
        since = v["timestamp"]
    return datetime.fromisoformat(since.replace("Z", "+00:00"))
