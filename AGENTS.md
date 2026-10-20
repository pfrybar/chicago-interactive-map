# AGENTS.md

Instructions for AI coding agents working on this repository. People should start with [README.md](README.md).

## Project overview

A walkable 3D map of Chicago for the web (mobile apps later), built only from open data.

- `pipeline/`: Python package `cim`. Turns downloaded sources into 3D Tiles. Runs in Docker, driven by Snakemake.
- `app/`: TypeScript viewer built on three.js and 3DTilesRendererJS, served by Vite.
- `eval/`: fixed viewpoints and a headless-Chromium screenshot harness.
- `agent/state/areas.yaml`: progress per community area.

The unit of work is a **community area** (Chicago's 77 official areas, numbered 1–77; the Loop is 32).

## Setup and commands

The host needs only bash and Docker. Never install Python or Node packages on the host; everything runs in containers.

```bash
./dev build [AREAS] [BUILD]   # e.g. ./dev build 32 dev, ./dev build 32,8, ./dev build all
FORCE=1 ./dev build ...       # rebuild tiles even if Snakemake thinks they're current
./dev app                     # viewer on http://localhost:5173 (container "web")
./dev eval [VIEWS]            # screenshots to eval/out/, exits 1 if a render budget is exceeded
./dev smoke                   # interaction test: picking and wall collisions
./dev test                    # pytest + tsc
./dev shell                   # bash in the pipeline container
```

Single pipeline steps: `docker compose run --rm pipeline python -m cim <fetch|osm|buildings|tiles|areas|locate|agent-state> ...`. Run `python -m cim --help` for arguments. `python -m cim locate <lat> <lon>` converts coordinates to local meters, which is handy for new viewpoints.

Dependency versions are pinned exactly. When adding or upgrading one, pick a release at least two weeks old.
- Python: add it to `pipeline/requirements.in`, rebuild the image, then regenerate `pipeline/requirements.txt` with `docker compose run --rm pipeline pip freeze` (keep the header comment).
- Node: edit `app/package.json` and refresh `app/pnpm-lock.yaml` with `docker compose run --rm --no-deps web corepack pnpm install`.

## Code style

- **Python:** 3.12, type hints on public functions, small modules with one job each (see `pipeline/cim/`). Geometry stays in shapely/geopandas until it becomes a mesh in `mesh.py`. Put a one-line docstring on each module and on any function whose purpose isn't obvious. Comments explain why, not what.
- **TypeScript:** strict mode, ES modules, single quotes, no semicolons, 2-space indent. Plain three.js; no UI framework until the UI needs one. All styling lives in `app/src/style.css`, with colors as CSS variables for light and dark themes.
- Match the surrounding code's naming and comment density. Don't add abstractions for a single use.

## Testing instructions

- `./dev test` must pass before every commit. After changing the viewer's controls or picking, also run `./dev smoke` (opens a building card, walks into a wall).
- Add a pytest for new pipeline logic in `pipeline/tests/`. Geometry tests use small synthetic shapes, not downloaded data.
- **Any change that affects what's rendered** (pipeline output, materials, lighting, camera) needs `./dev eval` before and after. Look at the screenshots in `eval/out/`, and include the relevant before/after images and the draw-call and triangle numbers in the PR description. `UI=1` keeps the panels in the screenshots; `PHONE=1` uses a phone-sized touch viewport (pass them with `docker compose run --rm -e UI=1 eval node render.mjs`).
- To add a viewpoint, edit `eval/viewpoints.json`. Coordinates are local meters (see below). Name it after the place, not the feature being tested.
- `window.__cim` exposes readiness and render stats. `window.__cimDebug` exposes `{ stage, city, controls }` for inspecting the scene from the console or a Playwright script.

## Coordinates (read before touching geometry)

- **Local frame:** meters from State & Madison, **x east, y north, z up**. The pipeline, `viewpoints.json`, `overview.json` and the minimap all use it.
- **glTF and three.js world:** x east, **y up, z south**. `(x, y, z)_gltf = (east, up, -north)`. Converted in `pipeline/cim/mesh.py` and `app/src/frame.ts`.
- `tileset.json` carries an east-north-up → Earth-centered transform. The app undoes it with `frameMatrix()` in `app/src/city.ts`. The ground is treated as flat (transverse Mercator); curvature, about 30 m of drop at the city edge, is ignored. Per-tile transforms will be needed before mixing in globe-based data.

## Data sources

- Every source goes in `sources.yaml` with its license and attribution **before** any code uses it. Downloads go through `python -m cim fetch`, which records URL, time, size and sha256 in `sources.lock.json`. Commit the lock file.
- **Allowed imagery for agents:** Mapillary and KartaView (CC-BY-SA; log every image used in `data/attribution/`), Wikimedia Commons and Flickr images whose license allows modification (check each one), and photos contributed to this project.
- **Never use Google or Apple map content in any form:** Google Maps, Street View, Earth, Photorealistic 3D Tiles, Apple Maps, Look Around, Flyover. That includes viewing it to "just check" a building's color. Their terms forbid creating map content from it, and it would contaminate the OSM-derived data. Treat their domains as blocked in any browser tool.
- **Don't edit OpenStreetMap automatically.** If you find an OSM error (wrong height, missing building), record it as a note in `agent/state/areas.yaml` or an issue for a person to fix. OSM's Automated Edits code of conduct applies.
- Fixes to our own output will go in `data/overrides/<area>/`, keyed by the stable building `id` (`chi:<city bldg_id>` or `osm:<w|r><id>`). The pipeline doesn't read overrides yet; add that before the first verify pass. Never hand-edit tiles.

## The neighborhood walk

Agents improve the map one community area and one **pass** at a time. A pass is a type of work, such as `massing`, `verify`, `trees` or `recipes`.

1. Pick an area from `agent/state/areas.yaml`. Prefer areas marked `stale: true`, then `todo` in the current pass, in this order: Loop (32), Near North Side (8), Near West Side (28), Near South Side (33), then outward.
2. Set that pass to `in_progress` in the same branch.
3. Build just that area (`./dev build <n> <branch-name>`), do the work, run `./dev test` and `./dev eval`.
4. Open one PR per area and pass. Include what changed, before/after screenshots, metric changes (for example height error or buildings added), and anything left undone in `notes`.
5. Set the pass to `done` (or `needs_review` if a person should look) with the date.

Citywide systems (the L, tree generation, materials) are built as generators that run for every area. Don't build them by hand for one neighborhood. Per-area passes check and correct the generators' output.

## Gotchas

- The viewer's compose service is named `web`, not `app`: Chromium forces HTTPS for any host named `app`. Vite also needs `web` in `server.allowedHosts`.
- `/builds/*` is served from `data/builds` by the `serveBuilds` plugin in `app/vite.config.ts`, gzipped, in both dev and preview. Don't put builds under `app/public/`: `vite build` would copy over a gigabyte of tiles into `dist/`.
- The sky draws first (`renderOrder -20`) and the base plane second, both without writing depth. Anything drawn before the sky gets painted over.
- Ground layers never overlap (each has the layers above it cut out; see `LAYER_ORDER`), so they all sit at height 0. The plain base plane is drawn first without depth writes. Don't reintroduce stacked heights: phones with 16-bit depth buffers z-fight.
- Ground is triangulated in 125 m cells (`GROUND_CELL`, scaled up per level); earcut is unreliable on whole-tile polygons with many holes. `clip_by_rect` can return invalid geometry; use `_clip_cell` or `make_valid` after it.
- Missing OSM tags are `None`, not NaN (`load_features` normalizes this). Check with `is None` or truthiness.
- three.js r186 removed `PCFSoftShadowMap`; use `PCFShadowMap`. The shadow map is resized to the view each frame (`followShadows`).
- Tiles form a quadtree (`pipeline/cim/grid.py`). The grid is anchored 32 km southwest of the origin, so the whole city has one root; keys are `Key(level, i, j)` and their `bounds` include that offset. Never compute tile indices with `floor(x / size)`; use `grid.index`.
- What each level keeps is set by `LEVEL_BUILDINGS` in `tiles.py` and `LEVEL_LAYERS` in `ground.py`. When you change them, check `city-overview` and a few mid-distance viewpoints in `./dev eval`.
- Tiles are quantized (`KHR_mesh_quantization`). Positions in tile geometry are 16-bit integers scaled by the node transform, so always go through `matrixWorld` (raycasts already do).
- `./dev eval` skips viewpoints whose `area` isn't in the current build; `area: null` always runs.
- Snakemake rules list their own modules as inputs (`code(...)` in the Snakefile). When a step starts using a new module, add it there, or edits to it won't trigger a rebuild.
- Inside containers the host UID has no user name. `USER` is set for Snakemake, and the `dev` script exports `UID`/`GID` for compose.

## Commits and pull requests

- Commit often: one logical change per commit, with a message that explains why.
- AI-assisted commits end with a `Co-Authored-By:` trailer naming the model. Don't add session-link trailers.
- Never commit anything under `data/` except `data/overrides/` and `data/attribution/`, and never commit secrets. API keys (for example a future CTA Train Tracker key) come from environment variables.
