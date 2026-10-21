"""Street-level photos near a point, for checking the map (Mapillary and KartaView).

    python -m cim images --at=-506,409            # list and download photos near local x,y
    python -m cim images --at=-506,409 --radius 80   (use --at=: x is often negative)
    python -m cim images --record mapillary:123   # log a photo used as evidence

Downloads go to data/work/imagery/<x>_<y>/ (not committed). Photos used as evidence
in overrides or notes must be recorded: `--record` appends to
data/attribution/images.jsonl, which is committed (CC-BY-SA requires credit).

Mapillary needs a token in the MAPILLARY_TOKEN environment variable (put it in
.env at the repo root; never commit it). KartaView needs nothing.
Google and Apple imagery must never be used (see AGENTS.md).
"""
import json
import math
import os
from datetime import datetime, timezone

import requests
from pyproj import Transformer

from .config import DATA, WORK
from .frame import local_frame

IMAGES = WORK / "imagery"
LEDGER = DATA / "attribution" / "images.jsonl"


def _lonlat(x: float, y: float) -> tuple[float, float]:
    return Transformer.from_crs(local_frame().crs, "EPSG:4326", always_xy=True).transform(x, y)


def _local(lon: float, lat: float) -> tuple[float, float]:
    return Transformer.from_crs("EPSG:4326", local_frame().crs, always_xy=True).transform(lon, lat)


def kartaview(lon: float, lat: float, radius: float) -> list[dict]:
    r = requests.post("https://api.openstreetcam.org/1.0/list/nearby-photos/",
                      data={"lat": lat, "lng": lon, "radius": radius}, timeout=30)
    r.raise_for_status()
    out = []
    for p in r.json().get("currentPageItems", []):
        host, _, path = p["lth_name"].partition("/")
        out.append({
            "id": f"kartaview:{p['id']}", "lon": float(p["lng"]), "lat": float(p["lat"]),
            "heading": float(p["heading"]) if p.get("heading") else None,
            "captured": (p.get("shot_date") or "")[:10], "author": p.get("username"),
            "panorama": p.get("projection") == "SPHERE",
            "url": f"https://{host}.openstreetcam.org/{path}",
            "page": f"https://kartaview.org/details/{p['sequence_id']}/{p['sequence_index']}",
            "license": "CC-BY-SA-4.0",
        })
    return out


def mapillary(lon: float, lat: float, radius: float) -> list[dict]:
    token = os.environ.get("MAPILLARY_TOKEN")
    if not token:
        return []
    dlat = radius / 111_320
    dlon = radius / (111_320 * math.cos(math.radians(lat)))
    r = requests.get("https://graph.mapillary.com/images", timeout=30, params={
        "access_token": token, "limit": 100,
        "bbox": f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}",
        "fields": "id,captured_at,compass_angle,computed_geometry,geometry,thumb_1024_url,creator,is_pano",
    })
    r.raise_for_status()
    out = []
    for p in r.json().get("data", []):
        g = (p.get("computed_geometry") or p.get("geometry"))["coordinates"]
        out.append({
            "id": f"mapillary:{p['id']}", "lon": g[0], "lat": g[1], "heading": p.get("compass_angle"),
            "captured": datetime.fromtimestamp(p["captured_at"] / 1000, timezone.utc).date().isoformat()
            if p.get("captured_at") else "",
            "author": (p.get("creator") or {}).get("username"), "panorama": bool(p.get("is_pano")),
            "url": p.get("thumb_1024_url"), "page": f"https://www.mapillary.com/app/?pKey={p['id']}",
            "license": "CC-BY-SA-4.0",
        })
    return out


def near(x: float, y: float, radius: float = 60.0, limit: int = 12, download: bool = True) -> list[dict]:
    """Photos within `radius` m of local (x, y), nearest first, newest first among ties."""
    lon, lat = _lonlat(x, y)
    # KartaView returns nothing for small radii (even when photos are close), so ask wide and filter here.
    photos = mapillary(lon, lat, radius) + kartaview(lon, lat, max(radius * 2, 300))
    for p in photos:
        px, py = _local(p["lon"], p["lat"])
        p["x"], p["y"] = round(px, 1), round(py, 1)
        p["distance"] = round(math.hypot(px - x, py - y), 1)
        # Bearing from the photo to the point of interest, to compare with its heading.
        p["bearing_to_target"] = round((math.degrees(math.atan2(x - px, y - py)) + 360) % 360)
    photos = [p for p in photos if p["distance"] <= radius]
    photos.sort(key=lambda p: (round(p["distance"] / 10), -int(p["captured"][:4]) if p["captured"] else 0))
    photos = photos[:limit]
    if download and photos:
        folder = IMAGES / f"{round(x)}_{round(y)}"
        folder.mkdir(parents=True, exist_ok=True)
        for p in photos:
            path = folder / (p["id"].replace(":", "_") + ".jpg")
            if not path.exists() and p.get("url"):
                r = requests.get(p["url"], timeout=60)
                if r.ok:
                    path.write_bytes(r.content)
            p["file"] = str(path.relative_to(DATA.parent))
        (folder / "photos.json").write_text(json.dumps(photos, indent=1))
    return photos


def record(image_id: str) -> dict:
    """Append a photo used as evidence to the attribution ledger (once)."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if LEDGER.exists():
        seen = {json.loads(l)["id"] for l in LEDGER.read_text().splitlines() if l.strip()}
    if image_id in seen:
        return {"id": image_id}
    found = None
    for folder in IMAGES.glob("*/photos.json"):
        for p in json.loads(folder.read_text()):
            if p["id"] == image_id:
                found = p
    if found is None:
        raise SystemExit(f"{image_id} isn't in any downloaded photo list; look it up with --at first")
    entry = {k: found[k] for k in ("id", "author", "captured", "license", "page", "lon", "lat")}
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry
