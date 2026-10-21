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


def test_city_footprints_on_bare_ground_are_gone():
    import pandas as pd
    from cim.tiles import gone_city_footprints
    b = pd.DataFrame({"kind": ["city", "city", "city", "outline"], "fp_area": [500, 500, 500, 500],
                      "year_built": [1958, 2019, 1958, 1958]})
    density = pd.Series([0.0, 0.0, 3.0, 0.0])
    assert gone_city_footprints(b, density).tolist() == [True, False, False, False]


def test_crown_cells_separates_trees_from_poles():
    from cim.trees import MIN_CROWN_CELLS, crown_cells
    raw = np.zeros((20, 20), np.float32)
    raw[5, 5] = 9.0                              # a light pole: one tall cell
    yy, xx = np.mgrid[0:20, 0:20]
    raw[(yy - 13) ** 2 + (xx - 13) ** 2 <= 9] = 8.0   # a crown 6 m across
    raw[13, 13] = 10.0
    c = crown_cells(raw, np.array([5, 13]), np.array([5, 13]), np.array([9.0, 10.0]))
    assert c[0] < MIN_CROWN_CELLS <= c[1]


def test_tree_mask_covers_carriageways_and_the_l_but_not_sidewalks_or_tunnels():
    import pandas as pd
    import shapely
    from shapely.geometry import LineString
    from cim.trees import _carriageways_and_rail
    feats = gpd.GeoDataFrame(pd.DataFrame({
        "highway": ["secondary", "footway", "secondary", None],
        "railway": [None, None, None, "subway"],
        "lanes": ["4", None, None, None], "tunnel": [None, None, "yes", None],
        "bridge": [None, None, None, "yes"], "layer": [None, None, "-1", "2"],
    }, dtype=object), geometry=[LineString([(0, 0), (100, 0)]), LineString([(0, 10), (100, 10)]),
                  LineString([(0, 50), (100, 50)]), LineString([(0, 80), (100, 80)])])
    m = _carriageways_and_rail(feats, (-10, -10, 110, 110))
    assert shapely.contains_xy(m, 50, 4)        # inside a 4-lane carriageway (13.2 m wide)
    assert not shapely.contains_xy(m, 50, 10)   # a footway: street trees stand there
    assert not shapely.contains_xy(m, 50, 50)   # a road in a tunnel
    assert shapely.contains_xy(m, 50, 87)       # under the elevated structure, near its edge


def test_bridge_deck_fills_slots_between_lanes_and_sidewalks_over_water():
    import pandas as pd
    import shapely
    from shapely.geometry import LineString
    from cim.ground import _bridge_decks
    # A road bridge (3 lanes, 9.9 m) with two sidewalks whose centerlines are 8.7 m away.
    feats = gpd.GeoDataFrame(pd.DataFrame({
        "highway": ["secondary", "footway", "footway"], "lanes": ["3", None, None],
        "bridge": ["movable", "yes", "yes"], "tunnel": [None] * 3, "layer": ["1"] * 3,
    }, dtype=object), geometry=[LineString([(0, -30), (0, 30)]), LineString([(8.7, -30), (8.7, 30)]),
                                LineString([(-8.7, -30), (-8.7, 30)])])
    water = box(-50, -20, 50, 20)
    hw = feats["highway"]
    deck = _bridge_decks(feats, pd.Series(True, index=feats.index), hw, water)
    assert shapely.contains_xy(deck, 6.0, 0) and shapely.contains_xy(deck, -6.0, 0)   # the slots are filled
    assert not shapely.contains_xy(deck, 6.0, 25)                                    # but only over water
    assert not shapely.contains_xy(deck, 20.0, 0)                                    # and not beyond the deck


def test_rail_profile_ignores_spikes_and_limits_grade():
    from cim.rail import MAX_GRADE, fit_profile
    s = np.arange(0, 200, 2.0)
    z = np.full_like(s, 8.0)
    z[50:52] = 10.5                      # a train or truss: +2.5 m over 4 m (the Wells bridge kink)
    z[80:] += np.linspace(0, 0.8, 20)    # a real gentle rise at the end
    out = fit_profile(s, z)
    assert np.abs(out[45:57] - 8.0).max() < 0.3
    assert (np.abs(np.diff(out)) / np.diff(s)).max() <= MAX_GRADE + 1e-9
    assert out[-1] > 8.4                 # the real rise survives


def test_roof_stats_counts_unclassified_tower_tops_above_a_building_class_podium():
    import pandas as pd
    from cim.lidar import BUILDING, UNCLASSIFIED, Dem, roof_stats
    rng = np.random.default_rng(3)
    dem = Dem(np.zeros((80, 80), np.float32), -10, -10, 1.0)
    b = gpd.GeoDataFrame({"id": ["tower", "house"]}, geometry=[box(0, 0, 20, 20), box(30, 0, 40, 10)])
    # Tower: walls and podium classed building up to 20 m, the roof at 180 m unclassified.
    tx, ty = rng.uniform(1, 19, 3000), rng.uniform(1, 19, 3000)
    tz = np.r_[rng.uniform(5, 20, 2000), np.full(1000, 180.0)]
    tc = np.r_[np.full(2000, BUILDING), np.full(1000, UNCLASSIFIED)]
    # House: roof at 8 m classed building, a tree over it at 15 m classed vegetation.
    hx, hy = rng.uniform(31, 39, 800), rng.uniform(1, 9, 800)
    hz = np.r_[np.full(600, 8.0), np.full(200, 15.0)]
    hc = np.r_[np.full(600, BUILDING), np.full(200, 5)]
    # Low building next to a tower: the neighbor's facade (unclassified) along one edge.
    b = pd.concat([b, gpd.GeoDataFrame({"id": ["low"]}, geometry=[box(0, 30, 20, 50)])], ignore_index=True)
    lx, ly = np.r_[rng.uniform(1, 19, 1000), rng.uniform(0.6, 1.4, 300)], np.r_[rng.uniform(31, 49, 1000), rng.uniform(31, 49, 300)]
    lz = np.r_[np.full(1000, 14.0), rng.uniform(30, 220, 300)]
    lc = np.r_[np.full(1000, BUILDING), np.full(300, UNCLASSIFIED)]
    s = roof_stats(gpd.GeoDataFrame(b, geometry="geometry"), np.r_[tx, hx, lx], np.r_[ty, hy, ly], np.r_[tz, hz, lz],
                   np.r_[tc, hc, lc].astype(np.uint8), dem).set_index("id")
    assert s.loc["tower", "p95"] > 170
    assert s.loc["house", "p95"] < 9
    assert s.loc["low", "p95"] < 15
