"""A street photo and our map from the same spot, side by side.

    ./dev photomatch mapillary:1250387419400528
    ./dev photomatch mapillary:166553602701717 --toward=-188,955   (a panorama: look at a point)

`pose` (here) downloads a Mapillary photo and works out where its camera stood and
looked: the reconstructed position, heading and pitch from Mapillary's computed
rotation, and the field of view from its focal length. Panoramas are cut into a
regular 4:3 view in the requested direction. eval/photomatch.mjs renders the map
from that pose, and `compose` puts the two side by side, with the photo's credit.

Everything goes to data/work/photomatch/ (not committed). Photos used as evidence
still need `python -m cim images --record <id>`.
"""
import io
import json
import math
import os

import numpy as np
import requests
from PIL import Image, ImageDraw
from pyproj import Transformer
from scipy.spatial.transform import Rotation

from .config import WORK
from .frame import local_frame

OUT = WORK / "photomatch"
EYE = 1.7          # the map's walk-mode eye height; car-mounted cameras are a little higher
PANO_FOV = 75.0    # horizontal field of view cut from a panorama
FIELDS = ("id,camera_type,camera_parameters,computed_compass_angle,computed_geometry,computed_rotation,"
          "width,height,captured_at,creator,thumb_2048_url")


def _meta(image_id: str) -> dict:
    token = os.environ.get("MAPILLARY_TOKEN")
    if not token:
        raise SystemExit("MAPILLARY_TOKEN isn't set (put it in .env)")
    r = requests.get(f"https://graph.mapillary.com/{image_id}", timeout=30,
                     params={"access_token": token, "fields": FIELDS})
    r.raise_for_status()
    return r.json()


def _orientation(meta: dict) -> tuple[float, float]:
    """(heading, pitch) in degrees. computed_rotation is OpenSfM's world-to-camera
    rotation (world east, north, up; camera z forward)."""
    if meta.get("computed_rotation"):
        f = Rotation.from_rotvec(meta["computed_rotation"]).as_matrix().T @ np.array([0.0, 0.0, 1.0])
        return math.degrees(math.atan2(f[0], f[1])) % 360, math.degrees(math.asin(np.clip(f[2], -1, 1)))
    return float(meta.get("computed_compass_angle") or 0.0), 0.0


def _pano_view(img: Image.Image, yaw: float, pitch: float, hfov: float, width: int = 1600) -> Image.Image:
    """A perspective view cut from an equirectangular panorama. `yaw` is relative to
    the panorama's center column, in degrees."""
    height = width * 3 // 4
    src = np.asarray(img.convert("RGB"))
    h, w = src.shape[:2]
    f = (width / 2) / math.tan(math.radians(hfov) / 2)
    u, v = np.meshgrid(np.arange(width) - width / 2 + 0.5, np.arange(height) - height / 2 + 0.5)
    ray = np.stack([u, -v, np.full_like(u, f)], -1)                     # x right, y up, z forward
    p, yw = math.radians(pitch), math.radians(yaw)
    rx = np.array([[1, 0, 0], [0, math.cos(p), math.sin(p)], [0, -math.sin(p), math.cos(p)]])
    ry = np.array([[math.cos(yw), 0, math.sin(yw)], [0, 1, 0], [-math.sin(yw), 0, math.cos(yw)]])
    d = ray @ (ry @ rx).T
    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arctan2(d[..., 1], np.hypot(d[..., 0], d[..., 2]))
    col = ((lon / (2 * math.pi) + 0.5) * w).astype(int) % w
    row = np.clip(((0.5 - lat / math.pi) * h).astype(int), 0, h - 1)
    return Image.fromarray(src[row, col])


def pose(photo: str, toward: tuple[float, float] | None = None, heading: float | None = None) -> dict:
    """Download a Mapillary photo and write its camera pose for eval/photomatch.mjs."""
    source, _, image_id = photo.partition(":")
    if source != "mapillary":
        raise SystemExit("only mapillary:<id> photos are supported (KartaView has no camera model)")
    meta = _meta(image_id)
    lon, lat = meta["computed_geometry"]["coordinates"]
    x, y = Transformer.from_crs("EPSG:4326", local_frame().crs, always_xy=True).transform(lon, lat)
    cam_heading, cam_pitch = _orientation(meta)
    img = Image.open(io.BytesIO(requests.get(meta["thumb_2048_url"], timeout=60).content))
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"mapillary_{image_id}"
    if toward is not None:
        heading = math.degrees(math.atan2(toward[0] - x, toward[1] - y)) % 360
    if meta.get("camera_type") in ("spherical", "equirectangular"):
        look = cam_heading if heading is None else heading
        img = _pano_view(img, (look - cam_heading + 540) % 360 - 180, 0.0, PANO_FOV)
        name += f"_{round(look)}"
        hfov, pitch, look_heading = PANO_FOV, 0.0, look
    else:
        focal = float((meta.get("camera_parameters") or [0.85])[0])  # relative to the longer side
        hfov = math.degrees(2 * math.atan(0.5 / focal)) if img.width >= img.height else \
            math.degrees(2 * math.atan(0.5 * img.width / img.height / focal))
        pitch, look_heading = cam_pitch, cam_heading
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * img.height / img.width))
    img.convert("RGB").save(OUT / f"{name}.jpg", quality=90)
    out = {"id": photo, "name": name, "x": round(x, 2), "y": round(y, 2), "heading": round(look_heading, 2),
           "pitch": round(pitch, 2), "vfov": round(vfov, 2), "width": img.width, "height": img.height,
           "author": (meta.get("creator") or {}).get("username"), "captured": meta.get("captured_at"),
           "license": "CC-BY-SA-4.0", "page": f"https://www.mapillary.com/app/?pKey={image_id}"}
    (OUT / f"{name}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out))
    return out


def compose(name: str) -> str:
    """Photo | map render side by side, with the photo's credit. Returns the file written."""
    meta = json.loads((OUT / f"{name}.json").read_text())
    photo = Image.open(OUT / f"{name}.jpg").convert("RGB")
    render = Image.open(OUT / f"{name}_render.png").convert("RGB").resize(photo.size)
    from PIL import ImageFont
    font = ImageFont.load_default(size=max(14, photo.width // 70))
    pad, bar = 8, max(34, photo.width // 30)
    out = Image.new("RGB", (photo.width * 2 + pad, photo.height + bar), "white")
    out.paste(photo, (0, 0))
    out.paste(render, (photo.width + pad, 0))
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(meta["captured"] / 1000, timezone.utc).date() if meta.get("captured") else "?"
    text = (f"Left: {meta['id']} by {meta['author']}, {when}, {meta['license']} ({meta['page']}).   "
            f"Right: Chicago Interactive Map from the same spot, heading {meta['heading']:.0f}°.")
    ImageDraw.Draw(out).text((pad, photo.height + bar // 4), text, fill=(20, 20, 20), font=font)
    path = OUT / f"{name}_match.jpg"
    out.save(path, quality=88)
    print(path.relative_to(WORK.parent.parent))
    return str(path)
