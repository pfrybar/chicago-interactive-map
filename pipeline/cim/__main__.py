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
    s = sub.add_parser("buildings", help="build buildings for one community area")
    s.add_argument("--area", type=int, required=True)

    t = sub.add_parser("tiles", help="write a 3D Tiles build for one or more areas")
    t.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    t.add_argument("--build", default="dev")

    li = sub.add_parser("lidar", help="ground model and roof heights from lidar, per 2 km block")
    li.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    li.add_argument("--force", action="store_true", help="reprocess blocks that already have outputs")

    ro = sub.add_parser("roofs", help="LoD 2.2 roof shapes with roofer, per 2 km block")
    ro.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    ro.add_argument("--force", action="store_true", help="redo blocks that already have outputs")

    ra = sub.add_parser("rail", help="deck heights of elevated track from lidar, per 2 km block")
    ra.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    ra.add_argument("--force", action="store_true", help="redo blocks that already have outputs")

    st = sub.add_parser("structures", help="deck heights of road overpasses from lidar, per 2 km block")
    st.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    st.add_argument("--force", action="store_true", help="redo blocks that already have outputs")

    tr = sub.add_parser("trees", help="tree positions and sizes from lidar, per 2 km block")
    tr.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    tr.add_argument("--force", action="store_true", help="redo blocks that already have outputs")

    lf = sub.add_parser("lidar-fetch", help="download and thin the Cook County 2022 lidar tiles some areas need")
    lf.add_argument("--areas", required=True, help="comma-separated community area numbers, or 'all'")
    lf.add_argument("--jobs", type=int, help="tiles at once (default lidar.cook2022_downloads)")

    orth = sub.add_parser("ortho", help="check trees and footprints against the 2025 county orthophotos")
    orth.add_argument("--area", type=int, required=True)
    orth.add_argument("--no-review", action="store_true", help="skip the local review crops")

    im = sub.add_parser("images", help="street-level photos near a point (Mapillary, KartaView)")
    im.add_argument("--at", help="local x,y in meters")
    im.add_argument("--radius", type=float, default=60.0)
    im.add_argument("--record", help="log a photo id used as evidence, e.g. mapillary:123")

    sub.add_parser("areas", help="list community areas")
    loc = sub.add_parser("locate", help="convert latitude/longitude to local map meters (for viewpoints)")
    loc.add_argument("lat", type=float)
    loc.add_argument("lon", type=float)
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
    elif a.cmd == "tiles":
        from .tiles import build
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        build(areas, a.build)
    elif a.cmd == "areas":
        from .areas import load_areas
        for _, r in load_areas().iterrows():
            print(f"{r['number']:>3}  {r['name']}")
    elif a.cmd == "lidar":
        from . import lidar
        from .grid import BLOCK_LEVEL
        from .tiles import build_leaves, load_buildings
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        blocks = {leaf.ancestor(BLOCK_LEVEL) for leaf in build_leaves(areas)}
        lidar.run(blocks, load_buildings(areas), a.force)
        lidar.record_lock()
    elif a.cmd == "lidar-fetch":
        from .grid import BLOCK_LEVEL
        from .lidar2022 import fetch_for
        from .tiles import build_leaves
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        fetch_for({leaf.ancestor(BLOCK_LEVEL) for leaf in build_leaves(areas)}, a.jobs)
    elif a.cmd == "ortho":
        from .ortho import check_area
        check_area(a.area, review=not a.no_review)
    elif a.cmd == "roofs":
        from . import roofs
        from .grid import BLOCK_LEVEL
        from .tiles import apply_lidar, build_leaves, load_buildings
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        blocks = {leaf.ancestor(BLOCK_LEVEL) for leaf in build_leaves(areas)}
        roofs.run(blocks, apply_lidar(load_buildings(areas), blocks), a.force)
    elif a.cmd == "rail":
        from . import rail
        from .grid import BLOCK_LEVEL
        from .tiles import build_leaves
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        rail.run({leaf.ancestor(BLOCK_LEVEL) for leaf in build_leaves(areas)}, a.force)
    elif a.cmd == "structures":
        from . import structures
        from .grid import BLOCK_LEVEL
        from .tiles import build_leaves
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        structures.run({leaf.ancestor(BLOCK_LEVEL) for leaf in build_leaves(areas)}, a.force)
    elif a.cmd == "trees":
        from . import trees
        from .grid import BLOCK_LEVEL
        from .tiles import build_leaves
        areas = list(range(1, 78)) if a.areas == "all" else [int(x) for x in a.areas.split(",")]
        trees.run({leaf.ancestor(BLOCK_LEVEL) for leaf in build_leaves(areas)}, a.force)
    elif a.cmd == "images":
        from . import imagery
        if a.record:
            print(imagery.record(a.record))
        elif a.at:
            x, y = (float(v) for v in a.at.split(","))
            for p in imagery.near(x, y, a.radius):
                print(f"{p['id']:28s} {p['distance']:5.1f} m  heading {p['heading'] or 0:5.0f}°"
                      f"  toward target {p['bearing_to_target']:3d}°  {p['captured']}  "
                      f"{'360°' if p['panorama'] else ''}  {p.get('file', '')}")
    elif a.cmd == "locate":
        from pyproj import Transformer
        from .frame import local_frame
        x, y = Transformer.from_crs("EPSG:4326", local_frame().crs, always_xy=True).transform(a.lon, a.lat)
        print(f"[{x:.0f}, {y:.0f}]")
    elif a.cmd == "agent-state":
        from .agent_state import refresh
        refresh()


if __name__ == "__main__":
    main()
