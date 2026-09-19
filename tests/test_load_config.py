"""load_config 回归：三种研究区写法（aoi circle / aoi polygon / 旧版顶层 polygon）、
缺失研究区报错、origin 的 BD-09 -> WGS84 换算。"""
import json
import math

import pytest
from shapely.geometry import Polygon

import isochrone


def _write(tmp_path, cfg):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_config_circle(tmp_path):
    path = _write(tmp_path, {
        "origin": [120.21, 30.26],
        "aoi": {"type": "circle", "radius_km": 2.0},
    })
    origin, polygon, _ = isochrone.load_config(path)
    # origin 已换算为 WGS84
    assert origin == pytest.approx(isochrone.bd09_to_wgs84(30.26, 120.21))
    # 圆面积 ≈ π * dlat * dlon（度²；经度方向按纬度压缩）
    olat, _ = origin
    dlat = 2.0 / 110.574
    dlon = 2.0 / (111.320 * math.cos(math.radians(olat)))
    assert polygon.area == pytest.approx(math.pi * dlat * dlon, rel=0.01)


def test_load_config_aoi_polygon(tmp_path):
    coords = [[120.20, 30.25], [120.23, 30.25], [120.23, 30.28], [120.20, 30.28]]
    path = _write(tmp_path, {
        "origin": [120.21, 30.26],
        "aoi": {"type": "polygon", "coords": coords},
    })
    _, polygon, _ = isochrone.load_config(path)
    assert isinstance(polygon, Polygon)
    assert len(polygon.exterior.coords) == 5  # 4 顶点 + 自动闭合


def test_load_config_legacy_polygon_key(tmp_path):
    coords = [[120.20, 30.25], [120.23, 30.25], [120.23, 30.28], [120.20, 30.28]]
    path = _write(tmp_path, {"origin": [120.21, 30.26], "polygon": coords})
    _, polygon, _ = isochrone.load_config(path)
    assert isinstance(polygon, Polygon)
    assert polygon.equals(isochrone.polygon_from_coords(coords))


def test_load_config_missing_aoi_raises(tmp_path):
    path = _write(tmp_path, {"origin": [120.21, 30.26]})
    with pytest.raises(KeyError):
        isochrone.load_config(path)
