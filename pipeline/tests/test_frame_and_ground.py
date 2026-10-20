import numpy as np
from shapely.geometry import LineString, box

from cim.frame import LocalFrame
from cim.ground import lake_from_coastline


def test_enu_matrix_is_orthonormal_and_on_the_ellipsoid():
    m = LocalFrame(41.88202, -87.6278, 147.0).enu_to_ecef()
    r = m[:3, :3]
    assert np.allclose(r.T @ r, np.eye(3), atol=1e-12)
    assert 6.36e6 < np.linalg.norm(m[:3, 3]) < 6.37e6
    assert m[2, 2] > 0.6  # "up" at 42°N leans toward the north pole


def test_lake_is_on_the_right_of_the_coastline():
    # Coastline running north along x=0: land on the left (west), water on the right (east).
    extent = box(-100, -100, 100, 100)
    lake = lake_from_coastline([LineString([(0, -200), (0, 200)])], extent)
    assert abs(lake.area - 200 * 100) < 1
    assert lake.centroid.x > 0
