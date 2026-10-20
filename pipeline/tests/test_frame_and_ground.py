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


def test_stack_layers_never_overlap_and_fill_the_tile():
    from cim.ground import stack
    tile = box(0, 0, 100, 100)
    raw = {"road": box(40, -10, 60, 110), "water": box(0, 0, 70, 50), "green": box(-50, -50, 150, 150)}
    out = stack(raw, tile, ["road", "water", "green"])
    pieces = list(out.values())
    assert abs(sum(g.area for g in pieces) - tile.area) < 1e-6
    for i, a in enumerate(pieces):
        for b in pieces[i + 1:]:
            assert a.intersection(b).area < 1e-6
    assert abs(out["road"].area - 20 * 100) < 1e-6  # the road wins where it crosses water
