"""矢量等时圈导出回归：FeatureCollection 结构、分级标注、环切分与洞配对、无数据跳过。"""
import json

import numpy as np

import isochrone


def _grid_with_durations(sample_origin, sample_polygon):
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=0.01)
    d = np.hypot(gdf["lat"].values - sample_origin[0],
                 gdf["lon"].values - sample_origin[1])
    gdf["duration_min"] = (d * 1000).round(1)  # 0 ~ 20+ 分钟，跨多个 5 分钟档
    XI, YI, zz = isochrone.idw_grid(gdf, grid_deg=0.0006)
    zz = isochrone.mask_by_polygon(XI, YI, zz, sample_polygon)
    return XI, YI, zz


def test_export_featurecollection_structure(tmp_path, sample_origin, sample_polygon):
    XI, YI, zz = _grid_with_durations(sample_origin, sample_polygon)
    out = tmp_path / "iso.geojson"
    n = isochrone.export_isochrone_geojson(XI, YI, zz, str(out), interval=5.0)

    fc = json.loads(out.read_text(encoding="utf-8"))
    assert fc["type"] == "FeatureCollection"
    assert n == len(fc["features"]) > 0
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Polygon"
        assert f["properties"]["direction"] == "from"
        assert f["properties"]["minutes"].endswith("分钟")
    # 分级标注与图例同一套：有中间档（lo-hi）与最后档（≥ lo）
    labels = {f["properties"]["minutes"] for f in fc["features"]}
    assert any("–" in lab for lab in labels)
    assert any(lab.startswith("≥") for lab in labels)
    # lower_min 从 0 开始逐档递增
    lowers = sorted({f["properties"]["lower_min"] for f in fc["features"]})
    assert lowers[0] == 0.0


def test_export_no_valid_data_skips_file(tmp_path):
    XI, YI = np.meshgrid(np.linspace(0, 1, 3), np.linspace(0, 1, 3))
    zz = np.full(XI.shape, np.nan)
    out = tmp_path / "empty.geojson"
    n = isochrone.export_isochrone_geojson(XI, YI, zz, str(out), interval=5.0)
    assert n == 0
    assert not out.exists()


def test_split_path_rings_closes_and_drops_closepoly():
    """compound path 按 MOVETO 切环、去掉 CLOSEPOLY 占位、自动补闭合点。"""
    pts = np.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0],
                    [5, 5], [6, 5], [6, 6], [5, 5]], dtype=float)
    codes = np.array([1, 2, 2, 2, 79, 1, 2, 2, 79])
    rings = isochrone._split_path_rings(pts, codes)
    assert len(rings) == 2
    assert np.allclose(rings[0][0], rings[0][-1])
    assert np.allclose(rings[1][0], rings[1][-1])
    assert len(rings[0]) == 5 and len(rings[1]) == 4


def test_rings_to_polygons_assigns_hole():
    """洞环不单独成面：外环+洞 -> 1 个带 interiors 的 Polygon。"""
    outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    hole = [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]]
    polys = isochrone._rings_to_polygons([outer, hole])
    assert len(polys) == 1
    assert len(polys[0].interiors) == 1


def test_rings_to_polygons_hole_island():
    """洞中岛（三层嵌套）：外环 -> 洞 -> 岛，各自角色正确。"""
    outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    hole = [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]]
    island = [[4, 4], [4.5, 4], [4.5, 4.5], [4, 4.5], [4, 4]]
    polys = isochrone._rings_to_polygons([outer, hole, island])
    assert len(polys) == 2
    with_hole = [p for p in polys if len(p.interiors) == 1]
    assert len(with_hole) == 1
    assert with_hole[0].exterior.bounds[0] == 0  # 外环那个
