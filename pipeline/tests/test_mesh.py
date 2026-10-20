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
