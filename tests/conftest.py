"""
共享夹具：样例几何、FakeSession（模拟百度网络层，绝不真实调用 API）、
tmp csv 路径等。

设计要点（根据 isochrone.py 真实签名）：
- `_fetch_batch(session, sem, origin, dests, ak, tactics, direction)` 通过
  `session.get(url, params=..., timeout=...)` 取 JSON；我们直接喂 FakeSession。
- `batch_route(...)` 内部 `async with aiohttp.ClientSession(...) as session`，
  故用 monkeypatch 把 `aiohttp.ClientSession` 替换成返回 FakeSession 的工厂，
  即可在不触网的情况下驱动整条批量算路 / 续跑 / 指纹分支逻辑。
"""
import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon

# 确保项目根（isochrone.py / picker.py 所在目录）在 sys.path 上
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# 样例几何（WGS84 空间：lon/lat）
# --------------------------------------------------------------------------- #
SAMPLE_ORIGIN = (30.265, 120.215)  # (lat, lon)

SAMPLE_POLY_COORDS = [
    (120.20, 30.25),
    (120.23, 30.25),
    (120.23, 30.28),
    (120.20, 30.28),
    (120.20, 30.25),
]
SAMPLE_POLYGON = Polygon(SAMPLE_POLY_COORDS)


@pytest.fixture
def sample_origin():
    return SAMPLE_ORIGIN


@pytest.fixture
def sample_polygon():
    return SAMPLE_POLYGON


@pytest.fixture
def tmp_csv(tmp_path):
    return str(tmp_path / "durations.csv")


# --------------------------------------------------------------------------- #
# FakeSession：模拟 aiohttp.ClientSession
# --------------------------------------------------------------------------- #
class FakeResponse:
    """可被 `async with session.get(...) as resp:` 使用的假响应。"""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _ErrResp:
    """get() 返回此对象时，进入 `async with` 即抛出注入的异常（模拟网络异常）。"""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """模拟 aiohttp.ClientSession：注入预设 JSON / 异常 / 按批动态响应。

    - payload:     固定返回的百度 result JSON（dict）。
    - side_effect: callable(params) -> dict，优先级高于 payload，用于按批返回不同结果。
    - exc:         若设置，get() 返回会在 __aenter__ 抛错的响应（模拟网络异常）。
    - calls:       记录每次 get 的 params（含 origins/destinations），便于断言
                    「已完成点未被重复请求」或「请求未静默错位」。
    """

    def __init__(self, payload=None, side_effect=None, exc=None):
        self.payload = payload
        self.side_effect = side_effect
        self.exc = exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        if self.exc is not None:
            return _ErrResp(self.exc)
        if self.side_effect is not None:
            payload = self.side_effect(params)
        else:
            payload = self.payload
        return FakeResponse(payload)


@pytest.fixture
def fake_session(monkeypatch):
    """工厂：fake_session(payload=...) / fake_session(side_effect=...) ，
    自动 monkeypatch aiohttp.ClientSession 使其返回对应的 FakeSession。"""
    import aiohttp

    box = {}

    def _factory(payload=None, side_effect=None, exc=None):
        fs = FakeSession(payload=payload, side_effect=side_effect, exc=exc)
        # batch_route 内部 `async with aiohttp.ClientSession(connector=...) as session`
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: fs)
        box["fs"] = fs
        return fs

    return _factory
