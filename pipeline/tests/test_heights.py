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
