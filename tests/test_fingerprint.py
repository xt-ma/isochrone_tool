"""配置指纹回归：敏感性 / 稳定性 / 不变性 / 12 位 hex。"""
import inspect

import pytest

import isochrone
from tests.conftest import SAMPLE_ORIGIN, SAMPLE_POLYGON


def _fp(origin=SAMPLE_ORIGIN, polygon=SAMPLE_POLYGON, cell=0.0028, tactics=11):
    return isochrone._config_fingerprint(origin, polygon, cell, tactics)


def test_fingerprint_12_hex():
    fp = _fp()
    assert len(fp) == 12
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_sensitivity():
    """origin / polygon / cell_deg / tactics 任一改变，指纹必变。"""
    base = _fp()
    assert _fp(origin=(SAMPLE_ORIGIN[0] + 0.001, SAMPLE_ORIGIN[1])) != base
    shifted = SAMPLE_POLYGON.buffer(0.001)
    assert _fp(polygon=shifted) != base
    assert _fp(cell=0.005) != base
    assert _fp(tactics=12) != base


def test_fingerprint_stability():
    """相同输入必得相同指纹。"""
    assert _fp() == _fp()


def test_fingerprint_invariance_to_render_params():
    """指纹函数只接受 (origin, polygon, cell_deg, tactics) 四个形参，
    grid_deg / interval / cmap / basemap / max_minutes / direction
    不在签名内，故不可能污染指纹（不变性保证）。"""
    params = set(inspect.signature(isochrone._config_fingerprint).parameters)
    assert params == {"origin", "polygon", "cell_deg", "tactics"}
