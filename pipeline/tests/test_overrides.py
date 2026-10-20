import geopandas as gpd
import pytest
from shapely.geometry import box

from cim import overrides


def _buildings():
    return gpd.GeoDataFrame({
        "id": ["chi:1", "chi:2"], "osm_id": [None, None], "kind": ["outline", "outline"], "name": [None, None],
        "height": [10.0, 20.0], "min_height": [0.0, 0.0], "height_source": ["lidar", "lidar"],
        "year_built": [None, None], "area": [32, 32], "cx": [5.0, 25.0], "cy": [5.0, 5.0],
        "fp_area": [100.0, 100.0], "base": [0.0, 0.0], "height_prev": [8.0, 18.0],
        "source_prev": ["default", "default"], "flat_roof": [False, False],
    }, geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)])


def test_apply_height_hide_and_add(tmp_path, monkeypatch):
    monkeypatch.setattr(overrides, "OVERRIDES", tmp_path)
    (tmp_path / "32.yaml").write_text("""
buildings:
  chi:1: {height: 12.5, reason: crane on the roof}
  chi:2: {hide: true, reason: demolished in 2019}
add:
  - {id: "fix:32:1", footprint: [[40, 0], [50, 0], [50, 10], [40, 10]], height: 4, reason: missing garage}
""")
    out = overrides.apply(_buildings(), [32], lambda x, y: 1.5)
    assert out.set_index("id").loc["chi:1", "height"] == 12.5
    assert "chi:2" not in set(out["id"])
    added = out.set_index("id").loc["fix:32:1"]
    assert added["base"] == 1.5 and added["height_source"] == "override"


def test_entries_need_a_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(overrides, "OVERRIDES", tmp_path)
    (tmp_path / "32.yaml").write_text("buildings:\n  chi:1: {height: 12}\n")
    with pytest.raises(overrides.OverrideError):
        overrides.load(32)
