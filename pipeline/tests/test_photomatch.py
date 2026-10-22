import numpy as np
from PIL import Image

from cim.photomatch import _orientation, _pano_view


def test_orientation_matches_mapillarys_compass():
    # Two real Mapillary photos: a dashcam on Lake Shore Drive and a panorama at Engine 42.
    heading, pitch = _orientation({"computed_rotation": [1.328459733579, 0.27947184722882, -0.17835723129955]})
    assert abs(heading - 342.05) < 0.01 and 10 < pitch < 15
    heading, _ = _orientation({"computed_rotation": [1.6146185386441, -0.010417728553976, 0.018967033431662]})
    assert abs(heading - 1.07) < 0.01


def test_pano_view_looks_where_it_is_told():
    # An equirectangular image whose columns encode their azimuth: red rises from the left edge.
    w, h = 720, 360
    img = np.zeros((h, w, 3), np.uint8)
    img[..., 0] = (np.arange(w) * 255 // (w - 1))[None, :]
    pano = Image.fromarray(img)
    center = lambda v: np.asarray(v)[v.height // 2, v.width // 2, 0]
    ahead = _pano_view(pano, 0, 0, 60, cam_heading=0, width=200)
    right = _pano_view(pano, 90, 0, 60, cam_heading=0, width=200)
    assert abs(int(center(ahead)) - 128) < 3        # straight ahead is the middle column
    assert abs(int(center(right)) - 191) < 3        # 90° right is three quarters across


def test_pano_view_uses_the_rotation_to_find_directions():
    # The same azimuth-coded panorama, from a camera facing east (rotation: north -> east).
    from scipy.spatial.transform import Rotation
    w, h = 720, 360
    img = np.zeros((h, w, 3), np.uint8)
    img[..., 0] = (np.arange(w) * 255 // (w - 1))[None, :]
    # World to camera for a level camera facing east: camera z = east, x = south, y = down.
    r = Rotation.from_matrix(np.array([[0, -1, 0], [0, 0, -1], [1, 0, 0]])).as_rotvec()
    east = _pano_view(Image.fromarray(img), 90, 0, 60, rotation=r, width=200)
    assert abs(int(np.asarray(east)[75, 100, 0]) - 128) < 3   # looking east is the panorama's middle
