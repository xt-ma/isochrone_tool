"""mask_by_polygon 回归：研究区外 NaN；带洞多边形的洞内一并 NaN（与渔网 contains 同语义）。"""
import numpy as np
from shapely.geometry import Polygon

import isochrone


def _square_grid():
    XI, YI = np.meshgrid(np.linspace(0.5, 9.5, 10), np.linspace(0.5, 9.5, 10))
    return XI, YI, np.ones(XI.shape)


def test_mask_keeps_inside():
    poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    XI, YI, zz = _square_grid()
    masked = isochrone.mask_by_polygon(XI, YI, zz, poly)
    assert (masked == 1).all()


def test_mask_respects_holes():
    poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                   [[(3, 3), (7, 3), (7, 7), (3, 7)]])
    XI, YI, zz = _square_grid()
    masked = isochrone.mask_by_polygon(XI, YI, zz, poly)
    # 网格点 0.5..9.5，洞 3..7 恰含 4x4=16 个点；(5.5, 5.5) 在洞内、(0.5, 0.5) 在洞外
    assert np.isnan(masked[5, 5])
    assert masked[0, 0] == 1.0
    assert int(np.isnan(masked).sum()) == 16
