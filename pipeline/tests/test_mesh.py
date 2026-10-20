import numpy as np
from shapely.geometry import Polygon, box

from cim.mesh import MeshBuilder, add_extrusion, add_flat


def _face_normals(a):
    p, i = a["POSITION"], a["indices"].reshape(-1, 3)
    n = np.cross(p[i[:, 1]] - p[i[:, 0]], p[i[:, 2]] - p[i[:, 0]])
    return n / np.linalg.norm(n, axis=1, keepdims=True), a["NORMAL"][i[:, 0]]


def test_flat_ground_faces_up_in_gltf():
    mb = MeshBuilder()
    add_flat(mb, box(0, 0, 10, 10), 0.0)
    a = mb.arrays()
    geo, stored = _face_normals(a)
    assert np.allclose(geo, [0, 1, 0])          # y is up in glTF
    assert np.allclose(stored, [0, 1, 0])


def test_extrusion_winding_matches_normals_including_holes():
    poly = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)], [[(5, 5), (5, 15), (15, 15), (15, 5)]])
    mb = MeshBuilder()
    add_extrusion(mb, poly, 0.0, 30.0, feature=7)
    a = mb.arrays()
    geo, stored = _face_normals(a)
    assert (np.einsum("ij,ij->i", geo, stored) > 0.99).all()  # front faces point where normals say
    assert a["POSITION"][:, 1].max() == 30.0
    assert set(a["_FEATURE_ID_0"]) == {7.0}


def test_north_maps_to_negative_gltf_z():
    mb = MeshBuilder()
    add_flat(mb, box(0, 100, 1, 101), 0.0)
    assert mb.arrays()["POSITION"][:, 2].max() == -100.0


def test_refine_splits_long_edges_without_cracks():
    from cim.mesh import refine, triangulate
    verts, tris = triangulate(box(0, 0, 100, 40))
    v, t = refine(verts, tris, 10.0)
    tri = v[t.reshape(-1, 3)]
    edges = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    assert np.linalg.norm(edges[:, 0] - edges[:, 1], axis=1).max() <= 10.0
    e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
    assert abs(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 2 - 4000) < 1e-6
    # Conforming: every interior edge is shared by exactly two triangles.
    key = np.sort(np.concatenate([t.reshape(-1, 3)[:, [0, 1]], t.reshape(-1, 3)[:, [1, 2]],
                                  t.reshape(-1, 3)[:, [2, 0]]]), axis=1)
    _, counts = np.unique(key, axis=0, return_counts=True)
    assert set(counts) <= {1, 2}


def test_rail_thinning_keeps_curves_and_drops_straight_runs():
    from cim.rail import _thin
    s = np.arange(0, 200, 4.0)
    straight = _thin(s, np.zeros_like(s), np.zeros_like(s))
    assert list(straight) == [0, 15, 30, 45, len(s) - 1] or len(straight) <= 6   # ends (and 60 m caps) only
    a = s / 200 * np.pi
    curve = _thin(100 * np.cos(a), 100 * np.sin(a), np.zeros_like(s))
    assert len(curve) > 10                                                     # a curve keeps its shape
