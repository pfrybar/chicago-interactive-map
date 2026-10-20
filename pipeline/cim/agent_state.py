"""agent/state/areas.yaml: which community areas have been built and walked."""
import json
from datetime import date

import yaml

from .areas import load_areas
from .config import BUILDS, REPO

STATE = REPO / "agent" / "state" / "areas.yaml"
HEADER = """\
# Progress of the neighborhood walk, one entry per community area.
# Agents update this file in the same PR as their changes. See AGENTS.md.
#   passes.<pass>.status: todo | in_progress | done | needs_review
#   stale: true when source data changed in the area since the last pass
"""


def refresh() -> None:
    state = yaml.safe_load(STATE.read_text()) if STATE.exists() else {}
    state = state or {}
    built = set()
    manifest = BUILDS / "manifest.json"
    if manifest.exists():
        current = json.loads(manifest.read_text())["current"]
        report = json.loads((BUILDS / current / "report.json").read_text())
        built = {a["number"] for a in report["areas"]}

    for _, r in load_areas().iterrows():
        n = int(r["number"])
        entry = state.setdefault(n, {"name": r["name"], "passes": {}, "stale": False, "notes": []})
        passes = entry.setdefault("passes", {})
        massing = passes.setdefault("massing", {"status": "todo"})
        if n in built and massing.get("status") != "done":
            passes["massing"] = {"status": "done", "at": date.today().isoformat()}
        passes.setdefault("verify", {"status": "todo"})

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(HEADER + yaml.safe_dump(dict(sorted(state.items())), sort_keys=False,
                                             allow_unicode=True))
    print(f"  {STATE.relative_to(REPO)}: {len(state)} areas, {len(built)} built")
