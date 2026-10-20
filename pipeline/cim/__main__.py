"""Command line: python -m cim <command> …  (see AGENTS.md for the full workflow)."""
import argparse


def main() -> None:
    p = argparse.ArgumentParser(prog="cim", description="Chicago Interactive Map data pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download a source from sources.yaml")
    f.add_argument("name")
    f.add_argument("--area", type=int)
    f.add_argument("--force", action="store_true")

    sub.add_parser("osm", help="cut Chicago out of the Illinois OSM extract")
    for name in ("buildings", "ground"):
        s = sub.add_parser(name, help=f"build {name} for one community area")
        s.add_argument("--area", type=int, required=True)

    t = sub.add_parser("tiles", help="write a 3D Tiles build for one or more areas")
    t.add_argument("--areas", required=True, help="comma-separated community area numbers")
    t.add_argument("--build", default="dev")

    sub.add_parser("areas", help="list community areas")
    sub.add_parser("agent-state", help="create or refresh agent/state/areas.yaml")

    a = p.parse_args()
    if a.cmd == "fetch":
        from .sources import fetch
        fetch(a.name, a.area, a.force)
    elif a.cmd == "osm":
        from .osm import build_features
        build_features()
    elif a.cmd == "buildings":
        from .buildings import build
        build(a.area)
    elif a.cmd == "ground":
        from .ground import build
        build(a.area)
    elif a.cmd == "tiles":
        from .tiles import build
        build([int(x) for x in a.areas.split(",")], a.build)
    elif a.cmd == "areas":
        from .areas import load_areas
        for _, r in load_areas().iterrows():
            print(f"{r['number']:>3}  {r['name']}")
    elif a.cmd == "agent-state":
        from .agent_state import refresh
        refresh()


if __name__ == "__main__":
    main()
