"""坐标换算回归：bd09<->wgs84 往返、已知样例量级、境外分支（不纠偏）。"""

import isochrone


def test_wgs84_to_bd09_then_back_roundtrip():
    """WGS84 -> BD09 -> WGS84 应几乎无损（绝对误差 < ~1e-4°）。"""
    lat, lon = 30.265, 120.215
    b_lat, b_lon = isochrone.wgs84_to_bd09(lat, lon)
    w_lat, w_lon = isochrone.bd09_to_wgs84(b_lat, b_lon)
    assert abs(w_lat - lat) < 1e-4
    assert abs(w_lon - lon) < 1e-4


def test_known_sample_shift_magnitude():
    """中国境内坐标转 BD09 应产生约 0.006° 的偏移（非平凡变换）。"""
    lat, lon = 39.9042, 116.4074  # 北京
    b_lat, b_lon = isochrone.wgs84_to_bd09(lat, lon)
    assert abs(b_lat - lat) > 1e-3
    assert abs(b_lon - lon) > 1e-3


def test_overseas_branch_no_shift():
    """境外坐标：wgs84_to_bd09 不纠偏，应原样返回。"""
    lat, lon = 51.5074, -0.1278  # 伦敦
    b_lat, b_lon = isochrone.wgs84_to_bd09(lat, lon)
    assert abs(b_lat - lat) < 1e-9
    assert abs(b_lon - lon) < 1e-9


def test_overseas_branch_bd09_to_wgs84_runs():
    """境外坐标走 bd09_to_wgs84 的境外分支（仅做 BD09->GCJ，跳过 GCJ->WGS84），
    应返回有限值且不抛异常（覆盖该分支）。"""
    lat, lon = 51.5074, -0.1278
    w_lat, w_lon = isochrone.bd09_to_wgs84(lat, lon)
    assert isinstance(w_lat, float) and isinstance(w_lon, float)
    # 境外分支未做完整逆变换，结果与原输入有偏差（证明分支确实被走到）
    assert abs(w_lat - lat) > 1e-3
