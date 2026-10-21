import json

from cim import osm_history


def test_height_tags_since_is_when_the_current_values_appeared(tmp_path, monkeypatch):
    monkeypatch.setattr(osm_history, "CACHE", tmp_path)
    (tmp_path / "w1.json").write_text(json.dumps([
        {"version": 1, "timestamp": "2013-03-01T00:00:00Z", "tags": {"building": "yes"}},
        {"version": 2, "timestamp": "2014-05-01T00:00:00Z", "tags": {"building": "yes", "building:levels": "21"}},
        {"version": 3, "timestamp": "2020-01-01T00:00:00Z", "tags": {"building": "apartments", "building:levels": "21"}},
    ]))
    assert osm_history.height_tags_since("w1").year == 2014   # a name or type edit doesn't reset it
    (tmp_path / "w2.json").write_text(json.dumps([
        {"version": 1, "timestamp": "2013-03-01T00:00:00Z", "tags": {"building:levels": "3"}},
        {"version": 2, "timestamp": "2019-06-01T00:00:00Z", "tags": {"building:levels": "55"}},
    ]))
    assert osm_history.height_tags_since("w2").year == 2019   # retagged after a new tower went up
