import pytest

from cim.heights import parse_length, resolve


@pytest.mark.parametrize("raw,meters", [
    ("120", 120.0), ("120 m", 120.0), ("12.5m", 12.5), ("394 ft", 394 * 0.3048),
    ("394'", 394 * 0.3048), ("12'6\"", 12 * 0.3048 + 6 * 0.0254), ("3,5", 3.5),
])
def test_parse_length(raw, meters):
    assert parse_length(raw) == pytest.approx(meters)


@pytest.mark.parametrize("raw", [None, "", "tall", "nan"])
def test_parse_length_rejects_junk(raw):
    assert parse_length(raw) is None


def test_resolve_prefers_osm_height_then_levels_then_city_stories():
    assert resolve({"height": "100", "building:levels": "10"}, 50)[2] == "osm:height"
    assert resolve({"building:levels": "10"}, 50) == (35.0, 0.0, "osm:levels")
    assert resolve({}, "5")[2] == "city:stories"
    assert resolve({"building": "garage"}, None) == (3.0, 0.0, "default")


def test_resolve_min_height_and_small_footprints():
    h, mh, _ = resolve({"min_height": "20", "height": "10"})
    assert mh == 20 and h > mh  # height never ends below min_height
    assert resolve({"building": "yes"}, None, footprint_m2=12)[0] == 3.5


def test_stray_tops_are_refereed_by_the_2017_survey(monkeypatch):
    import pandas as pd
    from cim import tiles
    monkeypatch.setattr(tiles, "lidar_source", lambda: "cook2022")
    # Highest 2017 return above each footprint's ground; the last one 2017 didn't see.
    monkeypatch.setattr(tiles, "_heights_2017", lambda blocks: pd.Series(
        {"house": 6.0, "tower": 160.0, "new": 4.0, "mapped": 4.0, "roof": 4.0}))
    b = pd.DataFrame({
        "id": ["house", "tower", "new", "mapped", "roof", "deck"],
        "p75": [5.0, 70.0, 80.0, 10.0, 5.0, 8.0], "base_min": 0.0,
        "classes": ["building+unclassified", "building+unclassified", "building+unclassified",
                    "all", "building", "all"],
        "n_top": [80, 300, 400, 50, 50, 40],
        "height_prev": [3.5, 7.0, 7.0, 90.0, 3.5, 3.5],
        "source_prev": ["default", "osm:levels", "osm:levels", "osm:levels", "default", "default"],
    })
    h, stray = tiles.without_stray_tops(b, pd.Series([95.0, 157.0, 93.0, 95.0, 60.0, 104.0]), set())
    # A noise cloud over a house falls to 2017's height; Kemper-like towers 2017 saw too,
    # a new building with a real roof, a mapped height and building-class points stay;
    # where 2017 saw nothing, a top far above the 75th percentile falls to it.
    assert stray.tolist() == [True, False, False, False, False, True]
    assert h.tolist() == [6.0, 157.0, 93.0, 95.0, 60.0, 8.0]
