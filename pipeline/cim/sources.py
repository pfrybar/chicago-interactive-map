"""Download sources listed in sources.yaml and record them in sources.lock.json."""
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import LOCK_FILE, RAW, load_sources


def _lock() -> dict:
    return json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else {}


def _write_lock(lock: dict) -> None:
    LOCK_FILE.write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")


def raw_path(name: str, area: int | None = None) -> Path:
    src = load_sources()[name]
    return RAW / name / src["file"].format(area=area)


def fetch(name: str, area: int | None = None, force: bool = False) -> Path:
    src = load_sources()[name]
    url = src["url"]
    if src.get("per_area"):
        if area is None:
            raise SystemExit(f"{name} is fetched per community area: pass --area")
        from .areas import area_bbox_lonlat

        west, south, east, north = area_bbox_lonlat(area, margin_deg=0.002)
        # Socrata within_box(column, top-left lat, top-left lon, bottom-right lat, bottom-right lon)
        url = url.replace("{bbox}", f"{north},{west},{south},{east}")
    dest = raw_path(name, area)
    if dest.exists() and not force:
        print(f"{dest.relative_to(RAW.parent.parent)} already downloaded (use --force to refresh)")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    sha = hashlib.sha256()
    size = 0
    print(f"Downloading {name} …")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                sha.update(chunk)
                size += len(chunk)
        final_url = r.url
        last_modified = r.headers.get("Last-Modified")
    shutil.move(tmp, dest)

    key = name if area is None else f"{name}:area-{area}"
    lock = _lock()
    lock[key] = {
        "url": final_url,
        "file": str(dest.relative_to(RAW.parent.parent)),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_modified": last_modified,
        "bytes": size,
        "sha256": sha.hexdigest(),
        "license": src["license"],
    }
    _write_lock(lock)
    print(f"  {size / 1e6:.1f} MB → {dest.relative_to(RAW.parent.parent)}")
    return dest
