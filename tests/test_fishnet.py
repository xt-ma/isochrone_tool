"""渔网生成回归：确定性、矩形/带洞/圆形点数正确。"""
import pytest
from shapely.geometry import Polygon

import isochrone


def _square(lo=0.0, hi=10.0):
    return Polygon([(lo, lo), (hi, lo), (hi, hi), (lo, hi), (lo, lo)])


def test_fishnet_deterministic_oid_set():
    poly = _square()
    a = isochrone.make_fishnet(poly, cell_deg=1.0)
    b = isochrone.make_fishnet(poly, cell_deg=1.0)
    assert list(a["oid"]) == list(range(len(a)))
    assert list(zip(a["lat"], a["lon"])) == list(zip(b["lat"], b["lon"]))


def test_fishnet_rectangle_count():
    """10x10 单位方格、cell=1.0 -> 严格内部恰好 10x10=100 个中心点。"""
    gdf = isochrone.make_fishnet(_square(0.0, 10.0), cell_deg=1.0)
    assert len(gdf) == 100


def test_fishnet_hole_count():
    """带洞多边形点数 = 外框点数 - 洞内点数（精确关系）。"""
    outer = _square(0.0, 10.0)
    hole = _square(3.0, 7.0)
    outer_with_hole = Polygon(outer.exterior.coords, [hole.exterior.coords])
    c_outer = len(isochrone.make_fishnet(outer, cell_deg=1.0))
    c_hole = len(isochrone.make_fishnet(hole, cell_deg=1.0))
    c_wh = len(isochrone.make_fishnet(outer_with_hole, cell_deg=1.0))
    assert c_wh == c_outer - c_hole
    assert c_outer == 100


def test_fishnet_circle_count():
    """圆形研究区：点数 > 0 且两次调用结果一致（确定性）。"""
    circle = isochrone.circle_polygon((30.265, 120.215), radius_km=2.0)
    a = isochrone.make_fishnet(circle, cell_deg=0.01)
    b = isochrone.make_fishnet(circle, cell_deg=0.01)
    assert len(a) > 0
    assert list(zip(a["lat"], a["lon"])) == list(zip(b["lat"], b["lon"]))
