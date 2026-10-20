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


def _dem_normals(dem, x, y) -> np.ndarray:
    """Surface normals of the elevation model at (x, y), from central differences."""
    h = 2.0
    gx = (dem.sample(x + h, y) - dem.sample(x - h, y)) / (2 * h)
    gy = (dem.sample(x, y + h) - dem.sample(x, y - h)) / (2 * h)
    n = np.column_stack([-gx, -gy, np.ones_like(gx)])
    return n / np.linalg.norm(n, axis=1, keepdims=True)


def add_draped(mb: MeshBuilder, poly: Polygon, dem, max_len: float | None = None) -> None:
    """A ground polygon laid over the elevation model (vertex heights sampled from `dem`).

    Triangles are refined until no edge is longer than `max_len`, so the surface
    follows the ground between the polygon's own vertices.
    """
    verts, tris = triangulate(poly)
    if verts is None or not len(tris):
        return
    if max_len:
        verts, tris = refine(verts, tris, max_len)
    z = dem.sample(verts[:, 0], verts[:, 1])
    mb.add(np.column_stack([verts, z]), _dem_normals(dem, verts[:, 0], verts[:, 1]), tris)


def add_walls(mb: MeshBuilder, ring: np.ndarray, z_bottom: float, top, skip=None, max_len: float = 5.0) -> None:
    """Vertical walls from `z_bottom` up to `top(x, y)` along a closed ring.

    The ring runs counter-clockwise around the area the walls face (for water,
    the walls face the water). Edges for which `skip(p, q)` is true are left open.
    """
    pts = [ring[0]]
    for p, q in zip(ring[:-1], ring[1:]):
        n = max(1, int(np.ceil(np.linalg.norm(q - p) / max_len)))
        pts.extend(p + (q - p) * (k / n) for k in range(1, n + 1))
    pts = np.asarray(pts)
    p, q = pts[:-1], pts[1:]
    keep = np.linalg.norm(q - p, axis=1) > 1e-3
    if skip is not None:
        keep &= ~skip(p, q)
    p, q = p[keep], q[keep]
    if not len(p):
        return
    zp, zq = top(p[:, 0], p[:, 1]), top(q[:, 0], q[:, 1])
    tall = (np.maximum(zp, zq) > z_bottom + 0.15)
    p, q, zp, zq = p[tall], q[tall], zp[tall], zq[tall]
    if not len(p):
        return
    d = q - p
    n2 = np.column_stack([-d[:, 1], d[:, 0]]) / np.linalg.norm(d, axis=1, keepdims=True)  # toward the area
    k = len(p)
    pos = np.empty((k * 4, 3))
    pos[0::4, :2], pos[0::4, 2] = p, z_bottom
    pos[1::4, :2], pos[1::4, 2] = q, z_bottom
    pos[2::4, :2], pos[2::4, 2] = q, np.maximum(zq, z_bottom)
    pos[3::4, :2], pos[3::4, 2] = p, np.maximum(zp, z_bottom)
    nrm = np.zeros((k * 4, 3))
    nrm[:, :2] = np.repeat(n2, 4, axis=0)
    base = np.arange(k, dtype=np.uint32) * 4
    mb.add(pos, nrm, np.column_stack([base, base + 1, base + 2, base, base + 2, base + 3]).reshape(-1))


def refine(verts: np.ndarray, tris: np.ndarray, max_len: float, max_rounds: int = 8):
    """Split every triangle edge longer than `max_len` at its midpoint until none remain.

    Whether an edge splits depends only on the edge itself, so triangles that share
    an edge (including across cells) split it the same way: no cracks or T-junctions.
    """
    verts = np.asarray(verts, np.float64)
    tris = np.asarray(tris, np.int64).reshape(-1, 3)
    for _ in range(max_rounds):
        e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
        key = np.sort(e, axis=1)
        length = np.linalg.norm(verts[key[:, 0]] - verts[key[:, 1]], axis=1)
        long = length > max_len
        if not long.any():
            break
        uniq, inv = np.unique(key[long], axis=0, return_inverse=True)
        mid_index = np.arange(len(uniq)) + len(verts)
        verts = np.vstack([verts, (verts[uniq[:, 0]] + verts[uniq[:, 1]]) / 2])
        # Midpoint of each triangle edge (-1 when that edge isn't split).
        mids = np.full(len(e), -1, np.int64)
        mids[np.flatnonzero(long)] = mid_index[inv.reshape(-1)]
        n = len(tris)
        m01, m12, m20 = mids[:n], mids[n:2 * n], mids[2 * n:]
        out = []
        a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
        s01, s12, s20 = m01 >= 0, m12 >= 0, m20 >= 0
        k = s01.astype(int) + s12 + s20
        # Unsplit triangles stay as they are.
        out.append(tris[k == 0])
        # All three edges split: four triangles.
        t = k == 3
        out += [np.c_[a[t], m01[t], m20[t]], np.c_[m01[t], b[t], m12[t]],
                np.c_[m20[t], m12[t], c[t]], np.c_[m01[t], m12[t], m20[t]]]
        # One edge split: two triangles. Rotate so the split edge is (p, q) with apex r.
        for sel, (p, q, r, m) in (
            ((k == 1) & s01, (a, b, c, m01)),
            ((k == 1) & s12, (b, c, a, m12)),
            ((k == 1) & s20, (c, a, b, m20)),
        ):
            out += [np.c_[p[sel], m[sel], r[sel]], np.c_[m[sel], q[sel], r[sel]]]
        # Two edges split: three triangles. Rotate so the unsplit edge is (r, p).
        for sel, (p, q, r, mpq, mqr) in (
            ((k == 2) & ~s20, (a, b, c, m01, m12)),
            ((k == 2) & ~s01, (b, c, a, m12, m20)),
            ((k == 2) & ~s12, (c, a, b, m20, m01)),
        ):
            out += [np.c_[p[sel], mpq[sel], mqr[sel]], np.c_[mpq[sel], q[sel], mqr[sel]],
                    np.c_[p[sel], mqr[sel], r[sel]]]
        tris = np.concatenate(out)
    return verts, tris.astype(np.uint32)
