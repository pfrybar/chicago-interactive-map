import geopandas as gpd
import numpy as np
from shapely.geometry import box

from cim.lidar import BUILDING, GROUND, Dem, build_dem, roof_stats
from cim.roofs import _triangulate_surface


def test_dem_bilinear_sample():
    z = np.array([[0.0, 2.0], [4.0, 6.0]], np.float32)  # rows go north
    d = Dem(z, 0.0, 0.0, 2.0)
    assert np.isclose(d.sample(1.0, 1.0), 0.0)        # cell center of [0, 0]
    assert np.isclose(d.sample(2.0, 1.0), 1.0)        # halfway east
    assert np.isclose(d.sample(2.0, 2.0), 3.0)        # middle of all four


def test_dem_fills_gaps_and_ignores_ground_points_on_roofs():
    rng = np.random.default_rng(1)
    x, y = rng.uniform(0, 40, 4000), rng.uniform(0, 40, 4000)
    z = np.zeros_like(x)
    roof = (x > 10) & (x < 20) & (y > 10) & (y < 20)
    z[roof] = 8.0                                       # roof points misclassified as ground
    cls = np.full(len(x), GROUND, np.uint8)
    dem = build_dem(x, y, z, cls, (0, 0, 40, 40), footprints=np.array([box(10, 10, 20, 20)]))
    assert abs(float(dem.sample(15, 15))) < 0.5         # no mound under the building


def test_roof_points_go_to_the_smallest_footprint():
    outer, tower = box(0, 0, 50, 50), box(20, 20, 30, 30)
    b = gpd.GeoDataFrame({"id": ["outer", "tower"]}, geometry=[outer, tower])
    xs, ys = np.meshgrid(np.arange(0.5, 50, 1.0), np.arange(0.5, 50, 1.0))
    x, y = xs.ravel(), ys.ravel()
    in_tower = (x > 20) & (x < 30) & (y > 20) & (y < 30)
    z = np.where(in_tower, 100.0, 10.0)
    cls = np.full(len(x), BUILDING, np.uint8)
    dem = Dem(np.zeros((25, 25), np.float32), 0, 0, 2.0)
    stats = roof_stats(b, x, y, z, cls, dem).set_index("id")
    assert stats.loc["tower", "p95"] == 100.0
    assert stats.loc["outer", "p95"] == 10.0             # the tower's points don't count for the outline


def test_triangulate_planar_roof_surface():
    ring = np.array([[0, 0, 5], [10, 0, 5], [10, 10, 10], [0, 10, 10]], float)  # a pitched plane
    verts, tris, n = _triangulate_surface([ring])
    assert len(tris) == 6 and np.isclose(np.linalg.norm(n), 1.0)
    assert n[2] > 0 and n[1] < 0                          # faces up and toward -y (downhill)


def test_outline_with_few_parts_keeps_the_uncovered_rest():
    # Mirrors the Art Institute: an outline with one small mapped part.
    import shapely
    from cim.buildings import PARTS_COVER
    outline, part = box(0, 0, 100, 100), box(0, 0, 30, 30)
    rest = outline.difference(part)
    assert rest.area > (1 - PARTS_COVER) * outline.area      # kept, minus the part
    assert shapely.area(outline.difference(box(0, 0, 100, 96))) <= (1 - PARTS_COVER) * outline.area  # dropped


def test_block_fingerprint_changes_with_buildings():
    from cim.lidar import fingerprint
    a = gpd.GeoDataFrame({"id": ["a", "b"]}, geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)])
    assert fingerprint(a) == fingerprint(a.iloc[::-1])                 # order doesn't matter
    assert fingerprint(a) != fingerprint(a.iloc[:1])                   # a building dropped
    trimmed = a.copy()
    trimmed.loc[0, "geometry"] = box(0, 0, 10, 5)
    assert fingerprint(a) != fingerprint(trimmed)                      # a footprint changed
