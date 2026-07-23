"""_fetch_batch 单测：换算、direction 分隔符、网络异常、status!=0、
正常 1:1，以及【P0-3】返回条数 != 请求数时整批标记失败。

绝不真实调用百度 API：全部走 FakeSession（见 conftest）。
"""
import asyncio

import pytest

import isochrone
from tests.conftest import FakeSession


ORIGIN = (30.265, 120.215)  # (lat, lon)


async def test_fetch_batch_seconds_to_minutes():
    dests = [(30.26, 120.21), (30.27, 120.22)]
    payload = {"status": 0, "result": [{"duration": {"value": 600}}, {"duration": {"value": 120}}]}
    fs = FakeSession(payload=payload)
    out, status, msg = await isochrone._fetch_batch(
        fs, asyncio.Semaphore(1), ORIGIN, dests, "fake", 11, "from")
    assert status == 0
    assert out == [10.0, 2.0]  # 秒 -> 分


async def test_fetch_batch_separator_is_pipe():
    dests = [(30.26, 120.21), (30.27, 120.22), (30.28, 120.23)]
    payload = {"status": 0, "result": [{"duration": {"value": 600}} for _ in dests]}
    # from：origins 为单点，destinations 为多点 '|' 分隔
    fs = FakeSession(payload=payload)
    await isochrone._fetch_batch(fs, asyncio.Semaphore(1), ORIGIN, dests, "fake", 11, "from")
    p_from = fs.calls[-1]
    assert "|" in p_from["destinations"]
    assert ";" not in p_from["destinations"]
    # to：origins 为多点 '|' 分隔，destinations 为单点
    fs2 = FakeSession(payload=payload)
    await isochrone._fetch_batch(fs2, asyncio.Semaphore(1), ORIGIN, dests, "fake", 11, "to")
    p_to = fs2.calls[-1]
    assert "|" in p_to["origins"]
    assert ";" not in p_to["origins"]


async def test_fetch_batch_network_error():
    import aiohttp
    fs = FakeSession(exc=aiohttp.ClientError("boom"))
    out, status, msg = await isochrone._fetch_batch(
        fs, asyncio.Semaphore(1), ORIGIN, [(30.26, 120.21)], "fake", 11)
    assert out == [None]
    assert status == -1
    assert "boom" in msg


async def test_fetch_batch_status_not_zero():
    payload = {"status": 401, "message": "当前并发量已经超过约定并发配额"}
    fs = FakeSession(payload=payload)
    out, status, msg = await isochrone._fetch_batch(
        fs, asyncio.Semaphore(1), ORIGIN, [(30.26, 120.21)], "fake", 11)
    assert out == [None]
    assert status == 401
    assert "并发" in msg


async def test_fetch_batch_normal_1to1():
    dests = [(30.26, 120.21)]
    payload = {"status": 0, "result": [{"duration": {"value": 300}}]}
    fs = FakeSession(payload=payload)
    out, status, msg = await isochrone._fetch_batch(
        fs, asyncio.Semaphore(1), ORIGIN, dests, "fake", 11)
    assert out == [5.0]
    assert status == 0


async def test_fetch_batch_result_count_mismatch_P0_3():
    """【P0-3 核心】请求 5 个 dest，百度只回 4 条（缺中间第 3 条），
    返回条数 != 请求数 -> 整批 [None]*5，status=-2，msg='result_count_mismatch'，
    绝不按末尾补 None 后写盘（避免 oid↔时长错位）。
    """
    dests = [(30.26 + i * 0.001, 120.21 + i * 0.001) for i in range(5)]
    # 只回 4 条结果（中间缺失）
    payload = {"status": 0, "result": [
        {"duration": {"value": 600}},
        {"duration": {"value": 660}},
        {"duration": {"value": 720}},
        {"duration": {"value": 780}},
    ]}
    fs = FakeSession(payload=payload)
    out, status, msg = await isochrone._fetch_batch(
        fs, asyncio.Semaphore(1), ORIGIN, dests, "fake", 11, "from")
    assert out == [None, None, None, None, None]
    assert status == -2
    assert msg == "result_count_mismatch"
