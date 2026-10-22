import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, box

from cim import structures
from cim.lidar import Dem


def _feats(rows, geoms):
    cols = ["highway", "bridge", "layer", "tunnel", "covered", "natural", "waterway", "landuse", "lanes", "width", "osm_id"]
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], dtype=object)
    return gpd.GeoDataFrame(df, geometry=geoms)


def test_bridges_over_water_stay_on_the_ground_others_become_overpasses():
    feats = _feats([
        {"osm_id": "w1", "highway": "secondary", "bridge": "yes", "layer": "1"},           # over the river
        {"osm_id": "w2", "highway": "motorway_link", "bridge": "yes", "layer": "2"},       # a flyover
        {"osm_id": "w3", "highway": "secondary"},                                          # not a bridge
        {"osm_id": "w9", "natural": "water"},
    ], [LineString([(0, 0), (0, 100)]), LineString([(200, 0), (200, 100)]), LineString([(300, 0), (300, 100)]),
        box(-50, 10, 50, 90)])
    ways = structures.overpasses(feats, (-100, -100, 400, 200))
    assert list(ways["osm_id"]) == ["w2"] and ways["layer"].iloc[0] == 2


def test_deck_heights_follow_the_profile_or_rise_by_layer():
    dem = Dem(np.zeros((50, 50), np.float32), -10, -10, 4.0)
    s = np.arange(0, 100, 4.0)
    x, y = np.zeros_like(s), s
    measured = pd.DataFrame({"s": s, "z": 7.0 + 0.01 * s})
    assert np.allclose(structures.deck_heights(s, x, y, 1, measured, dem), 7.0 + 0.01 * s, atol=0.15)
    assert np.allclose(structures.deck_heights(s, x, y, 2, pd.DataFrame(columns=["s", "z"]), dem),
                       2 * structures.DEFAULT_RISE)


def test_trench_walls_rise_beside_a_sunken_expressway_only():
    z = np.zeros((100, 100), np.float32)
    z[:, 45:55] = -6.0                                  # a 20 m wide trench along x = -10..10
    dem = Dem(z, -100, -100, 2.0)
    feats = _feats([{"osm_id": "w1", "highway": "motorway", "lanes": "4"},
                    {"osm_id": "w2", "highway": "secondary"}],
                   [LineString([(0, -80), (0, 80)]), LineString([(60, -80), (60, 80)])])
    mb = structures.trench_walls(feats, dem, (-100, -100, 100, 100), 0)
    pos = np.concatenate(mb.positions)
    assert len(pos) and np.abs(np.abs(pos[:, 0]) - 6.6).max() < 0.2      # on the expressway's edges only
    assert pos[:, 2].max() > -1 and pos[:, 2].min() < -4                 # from the road up to the street
