"""_fetch_batch 单测：坐标换算、direction 分隔符、网络异常、status!=0、
正常 1:1、返回条数 != 请求数时整批标记失败，以及 batch_route 对该防护的集成。

绝不真实调用百度 API：全部走 FakeSession（见 conftest）。
"""
import asyncio

import pandas as pd

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


async def test_fetch_batch_result_count_mismatch():
    """请求 5 个 dest，百度只回 4 条（缺中间第 3 条）：
    返回条数 != 请求数 -> 位置映射不可信，整批 [None]*5、status=-2、
    msg='result_count_mismatch'，绝不按末尾补 None 后写盘（避免 oid↔时长错位）。
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


# --------------------------------------------------------------------------- #
# batch_route 集成：某批返回条数 != 请求数时，该批整体不得写盘（防 oid 错位）
# --------------------------------------------------------------------------- #
def test_batch_route_skips_batch_on_count_mismatch(
    fake_session, sample_origin, sample_polygon, tmp_csv
):
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    n = len(gdf)
    batch_size = 2

    state = {"n": 0}

    def side_effect(params):
        state["n"] += 1
        ndest = params["destinations"].count("|") + 1
        if state["n"] == 1:
            # 第一批（2 个 dest）返回「中间缺失」：只回 1 条（含会被误用的错误值 999）
            return {"status": 0, "result": [{"duration": {"value": 999 * 60}}]}
        return {"status": 0, "result": [
            {"duration": {"value": int((10 + i) * 60)}} for i in range(ndest)]}

    fs = fake_session(side_effect=side_effect)

    # max_retry=1 让第一批只试一次即跳过；delay=0 加速
    asyncio.run(isochrone.batch_route(
        sample_origin, gdf, "fakeak", tactics=11, batch_size=batch_size,
        csv_path=tmp_csv, direction="from", polygon=sample_polygon,
        cell_deg=cell, origin_bd=(120.21, 30.26), force=False,
        delay=0.0, max_retry=1,
    ))

    df = pd.read_csv(tmp_csv)
    csv_oids = set(df["oid"].tolist())
    csv_durs = df["duration_min"].tolist()

    # 第一批 oid（0,1）整批缺失 -> 不应出现在 csv 中（未静默错位写盘）
    assert 0 not in csv_oids
    assert 1 not in csv_oids
    # 错误值 999（分钟）绝不能落到任何一行（证明没有把错位结果写进去）
    assert 999.0 not in csv_durs
    # 其余批次正常 -> 全部到位
    assert set(range(2, n)).issubset(csv_oids)
    # 请求批数 = ceil(n / batch_size)
    assert len(fs.calls) == (n + batch_size - 1) // batch_size
