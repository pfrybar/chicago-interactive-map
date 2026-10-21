import numpy as np
from shapely.geometry import box

from cim import water


def _reader(cls, z):
    def read(bounds, depth):
        rng = np.random.default_rng(0)
        x = rng.uniform(bounds[0], bounds[2], 2000)
        y = rng.uniform(bounds[1], bounds[3], 2000)
        return x, y, np.full(2000, z), np.full(2000, cls, np.uint8)
    return read


def test_lagoon_level_comes_from_its_water_returns():
    z, source, n = water.measure(box(0, 0, 200, 100), read=_reader(9, 1.16))
    assert (round(z, 2), source) == (1.16, "lidar") and n > 30


def test_ground_returns_stand_in_for_water_and_big_bodies_are_the_lake():
    assert water.measure(box(0, 0, 200, 100), read=_reader(2, -2.2))[:2] == (-2.2, "lidar-ground")
    z, source, _ = water.measure(box(0, 0, 2000, 1000), read=_reader(9, 3.0))   # 2 km²: the river system
    assert (z, source) == (water.lake_z(), "lake")
