"""Repository paths and config loading."""
from functools import lru_cache
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data"
RAW = DATA / "raw"
WORK = DATA / "work"
BUILDS = DATA / "builds"
LOCK_FILE = REPO / "sources.lock.json"


@lru_cache
def load_config() -> dict:
    return yaml.safe_load((REPO / "pipeline" / "config.yaml").read_text())


@lru_cache
def load_sources() -> dict:
    return yaml.safe_load((REPO / "sources.yaml").read_text())


def area_dir(area: int) -> Path:
    return WORK / f"area-{area}"
