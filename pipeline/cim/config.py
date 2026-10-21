"""Repository paths and config loading."""
import os
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


def lidar_source() -> str:
    """Which lidar survey to use: "ept2017" or "cook2022" (CIM_LIDAR_SOURCE overrides config)."""
    return os.environ.get("CIM_LIDAR_SOURCE") or load_config()["lidar"].get("source", "ept2017")


def lidar_work(name: str) -> Path:
    """Work folder for a lidar-derived step. Each survey has its own, so switching keeps the other's caches."""
    source = lidar_source()
    return WORK / name if source == "ept2017" else WORK / f"{name}-{source}"
