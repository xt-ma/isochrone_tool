"""render_map 回归：输出 HTML 含图例元素，ImageOverlay 层数 == 时间档数。"""
import numpy as np

import isochrone


def test_render_map_has_legend_and_band_overlays(tmp_path, sample_origin, sample_polygon):
    # 构造含 duration_min 的 gdf
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=0.01)
    d = np.hypot(gdf["lat"].values - sample_origin[0],
                 gdf["lon"].values - sample_origin[1])
    gdf["duration_min"] = (d * 1000).round(1)

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
