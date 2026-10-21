import laspy
import numpy as np
from pyproj import Transformer

from cim.frame import local_frame
from cim.lidar2022 import FT, STATE_PLANE, convert


def _write_tile(path, n=40_000):
    # A 100 ft square near State & Madison, in the county's state plane feet.
    to_sp = Transformer.from_crs(local_frame().crs, STATE_PLANE, always_xy=True)
    x0, y0 = to_sp.transform(0.0, 0.0)
    rng = np.random.default_rng(1)
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = np.array([0.001, 0.001, 0.001])
    header.offsets = np.array([x0, y0, 0.0])
    las = laspy.LasData(header)
    las.x = x0 + rng.random(n) * 100
    las.y = y0 + rng.random(n) * 100
    las.z = np.full(n, 600.0)  # feet above NAVD88
    las.classification = rng.choice([2, 6, 7, 11, 14, 18], n).astype(np.uint8)
    las.z[:100] = 6000.0  # stray returns far above any roof
    las.write(path)


def test_convert_thins_drops_noise_and_moves_to_local_meters(tmp_path):
    src, dest = tmp_path / "t.las", tmp_path / "t.laz"
    _write_tile(src)
    area_m2 = (100 * FT) ** 2
    kept = convert(src, dest, density=20)
    las = laspy.read(dest)
    c = np.asarray(las.classification)
    assert set(np.unique(c)) <= {1, 2, 6}              # low noise (7), wires (14) dropped; road -> ground, 18 -> 1
    assert np.asarray(las.z).max() < 460               # stray returns dropped
    expected = 20 * area_m2 * (4 / 6)                  # thinned, then 2 of 6 classes dropped
    assert 0.8 * expected < kept < 1.2 * expected
    assert abs(np.median(las.z) - (600 * FT - 181.0)) < 0.02   # NAVD88 feet -> local meters
    assert np.asarray(las.x).min() > -5 and np.asarray(las.x).max() < 40   # local meters near the origin
