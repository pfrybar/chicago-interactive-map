"""Triangle meshes from 2D polygons: flat ground pieces and extruded buildings.

Inputs are in the local frame (x east, y north, z up). Outputs are converted to
glTF's y-up convention: (x, y, z)_gltf = (east, up, -north).
"""
from dataclasses import dataclass, field

import mapbox_earcut as earcut
import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient


@dataclass
class MeshBuilder:
    positions: list = field(default_factory=list)
    normals: list = field(default_factory=list)
    indices: list = field(default_factory=list)
    feature_ids: list = field(default_factory=list)
    count: int = 0

    def add(self, pos: np.ndarray, nrm: np.ndarray, idx: np.ndarray, feature: int = 0) -> None:
        idx = _wind(pos, nrm, idx)
        self.positions.append(pos)
        self.normals.append(nrm)
        self.indices.append(idx + self.count)
        self.feature_ids.append(np.full(len(pos), feature, dtype=np.float32))
        self.count += len(pos)

    def arrays(self):
        if not self.count:
            return None
        pos = np.concatenate(self.positions).astype(np.float32)
        gl = np.stack([pos[:, 0], pos[:, 2], -pos[:, 1]], axis=1)
        nrm = np.concatenate(self.normals).astype(np.float32)
        gl_n = np.stack([nrm[:, 0], nrm[:, 2], -nrm[:, 1]], axis=1)
        return {
            "POSITION": gl,
            "NORMAL": gl_n,
            "indices": np.concatenate(self.indices).astype(np.uint32),
            "_FEATURE_ID_0": np.concatenate(self.feature_ids),
        }


def _wind(pos: np.ndarray, nrm: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Flip triangles whose winding disagrees with the intended normal (glTF wants CCW front faces).

    Winding is judged in the local right-handed frame; the y-up conversion
    (x, z, -y) is a proper rotation, so it preserves winding.
    """
    tri = idx.reshape(-1, 3)
    a, b, c = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    face_n = np.cross(b - a, c - a)
    flip = np.einsum("ij,ij->i", face_n, nrm[tri[:, 0]]) < 0
    tri[flip] = tri[flip][:, [0, 2, 1]]
    return tri.reshape(-1)


def _rings(poly: Polygon) -> list[np.ndarray]:
    poly = orient(poly, sign=1.0)  # exterior counter-clockwise, holes clockwise
    rings = [np.asarray(poly.exterior.coords)[:-1, :2]]
    rings += [np.asarray(r.coords)[:-1, :2] for r in poly.interiors]
    return [r for r in rings if len(r) >= 3]


def triangulate(poly: Polygon):
    rings = _rings(poly)
    if not rings:
        return None, None
    verts = np.concatenate(rings)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    tris = earcut.triangulate_float64(verts, ends)
    return verts, np.asarray(tris, dtype=np.uint32)


def add_flat(mb: MeshBuilder, poly: Polygon, z: float, feature: int = 0, up: bool = True) -> None:
    verts, tris = triangulate(poly)
    if verts is None or not len(tris):
        return
    pos = np.column_stack([verts, np.full(len(verts), z)])
    nrm = np.tile([0.0, 0.0, 1.0 if up else -1.0], (len(verts), 1))
    mb.add(pos, nrm, tris, feature)


def add_extrusion(mb: MeshBuilder, poly: Polygon, z0: float, z1: float, feature: int) -> None:
    """Prism from z0 to z1: walls, a roof, and a floor if the prism floats above the ground."""
    add_flat(mb, poly, z1, feature, up=True)
    if z0 > 0.25:
        add_flat(mb, poly, z0, feature, up=False)
    for ring in _rings(poly):
        p = ring
        q = np.roll(ring, -1, axis=0)
        d = q - p
        length = np.linalg.norm(d, axis=1)
        keep = length > 1e-3
        p, q, d, length = p[keep], q[keep], d[keep], length[keep]
        if not len(p):
            continue
        # Exterior is CCW and holes are CW, so (dy, -dx) always points out of the solid.
        n2 = np.column_stack([d[:, 1], -d[:, 0]]) / length[:, None]
        k = len(p)
        pos = np.empty((k * 4, 3))
        pos[0::4, :2], pos[0::4, 2] = p, z0
        pos[1::4, :2], pos[1::4, 2] = q, z0
        pos[2::4, :2], pos[2::4, 2] = q, z1
        pos[3::4, :2], pos[3::4, 2] = p, z1
        nrm = np.zeros((k * 4, 3))
        nrm[:, :2] = np.repeat(n2, 4, axis=0)
        base = np.arange(k, dtype=np.uint32) * 4
        idx = np.column_stack([base, base + 1, base + 2, base, base + 2, base + 3]).reshape(-1)
        mb.add(pos, nrm, idx, feature)
