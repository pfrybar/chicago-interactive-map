import laspy
import numpy as np
from pyproj import Transformer

from cim.frame import local_frame
from cim.lidar2022 import FT, STATE_PLANE, convert, drop_isolated


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


def test_drop_isolated_keeps_sparse_roofs_and_drops_lone_returns():
    rng = np.random.default_rng(2)
    # A dark tower roof: unclassified, one point per 5 m² over 40 × 40 m, at 180 ± 0.3 m.
    n = 320
    roof = (rng.random(n) * 40, rng.random(n) * 40, 180 + rng.normal(0, 0.3, n), np.ones(n, np.uint8))
    # Noise over a shed next to it: lone returns at random heights.
    k = 30
    noise = (100 + rng.random(k) * 40, rng.random(k) * 40, 10 + rng.random(k) * 250, np.ones(k, np.uint8))
    # Ground under both, which is never dropped however sparse.
    ground = (np.array([20.0, 120.0]), np.array([20.0, 20.0]), np.zeros(2), np.full(2, 2, np.uint8))
    x, y, z, c = (np.concatenate(p) for p in zip(roof, noise, ground))
    kx, ky, kz, kc = drop_isolated(x, y, z, c)
    assert (kz > 170).sum() >= 0.97 * n
    assert ((kx >= 100) & (kc == 1)).sum() <= 2
    assert (kc == 2).sum() == 2
