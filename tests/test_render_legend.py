"""render_map 回归：图例元素、ImageOverlay 层数，以及叠加图像地理范围与栅格一致（防错位）。"""
import re

import numpy as np
import pytest

import isochrone


def _gdf_with_durations(sample_origin, sample_polygon):
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=0.01)
    d = np.hypot(gdf["lat"].values - sample_origin[0],
                 gdf["lon"].values - sample_origin[1])
    gdf["duration_min"] = (d * 1000).round(1)
    return gdf


def test_render_map_has_legend_and_band_overlays(tmp_path, sample_origin, sample_polygon):
    gdf = _gdf_with_durations(sample_origin, sample_polygon)

    XI, YI, zz = isochrone.idw_grid(gdf, grid_deg=0.0006)
    zz = isochrone.mask_by_polygon(XI, YI, zz, sample_polygon)

    out = tmp_path / "iso.html"
    isochrone.render_map(
        sample_origin, sample_polygon, XI, YI, zz,
        out_html=str(out), interval=5.0, max_minutes=15.0,
    )

    html = out.read_text(encoding="utf-8")
    # 图例元素存在
    assert "isochrone-legend" in html
    # max_minutes=15, interval=5 -> bounds=[0,5,10,15] -> 3 个时间档 -> 3 个 ImageOverlay
    assert html.count("imageOverlay(") == 3


def test_overlay_bounds_follow_grid_extent(tmp_path, sample_origin, sample_polygon):
    """错位修复回归：ImageOverlay 的地理范围必须 == 栅格实际范围（XI/YI），
    而不是研究区 bounds。刻意丢掉最西端采样点使有效范围收缩，二者不再一致；
    若用研究区 bounds 映射图像，色面会被拉伸错位。"""
    gdf = _gdf_with_durations(sample_origin, sample_polygon)
    gdf = gdf[gdf["lon"] > gdf["lon"].min()].reset_index(drop=True)  # 丢最西列

    XI, YI, zz = isochrone.idw_grid(gdf, grid_deg=0.0006)
    zz = isochrone.mask_by_polygon(XI, YI, zz, sample_polygon)

    out = tmp_path / "iso.html"
    isochrone.render_map(
        sample_origin, sample_polygon, XI, YI, zz,
        out_html=str(out), interval=5.0, max_minutes=15.0,
    )
    html = out.read_text(encoding="utf-8")

    matches = re.findall(
        r"L\.imageOverlay\(\s*\"[^\"]*\",\s*"
        r"\[\[([-0-9.eE]+), ?([-0-9.eE]+)\], ?\[([-0-9.eE]+), ?([-0-9.eE]+)\]\]",
        html,
    )
    assert matches, "HTML 中未找到 imageOverlay 的 bounds"
    assert len(matches) == 3  # max_minutes=15, interval=5 -> 3 个时间档
    for m in matches:
        lat0, lon0, lat1, lon1 = (float(v) for v in m)
        assert lat0 == pytest.approx(float(YI.min()), abs=1e-9)
        assert lon0 == pytest.approx(float(XI.min()), abs=1e-9)
        assert lat1 == pytest.approx(float(YI.max()), abs=1e-9)
        assert lon1 == pytest.approx(float(XI.max()), abs=1e-9)
    # 栅格范围与研究区 bounds 确实不同（否则该测试失去意义）
    assert float(XI.min()) > sample_polygon.bounds[0]
