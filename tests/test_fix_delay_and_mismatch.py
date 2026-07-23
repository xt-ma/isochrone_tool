"""针对本次改动的新增专门用例：
(a) --delay 接线（run_pipeline 透传 + main 透传 + argparse 不接收 --concurrency）
(b) P0-3 修复验证：批量算路「中间缺失」时整批标记失败、绝不把错位时长写盘
(c) pandas 导入冒烟（requirements.txt 已加 pandas>=2.0）
(P1-2) main 同时给 --config 与 --origin/--polygon 时的告警

绝不真实调用百度 API。
"""
import asyncio
import json
import sys

import pytest

import isochrone
from shapely.geometry import Polygon


# --------------------------------------------------------------------------- #
# (c) pandas 导入冒烟
# --------------------------------------------------------------------------- #
def test_pandas_import():
    import pandas as pd  # noqa: F401
    import isochrone  # 已随 conftest 导入；此处确保 pandas 依赖可用
    assert hasattr(isochrone, "make_fishnet")


# --------------------------------------------------------------------------- #
# (a) --delay 接线
# --------------------------------------------------------------------------- #
def test_run_pipeline_delay_passthrough(monkeypatch):
    origin = (30.265, 120.215)
    polygon = Polygon([(120.20, 30.25), (120.23, 30.25),
                       (120.23, 30.28), (120.20, 30.28)])

    captured = {}

    async def fake_batch_route(origin, gdf, ak, tactics=11, batch_size=50,
                               concurrency=1, delay=3.0, **kw):
        captured["delay"] = delay
        return {int(r.oid): 3.0 for r in gdf.itertuples()}

    monkeypatch.setattr(isochrone, "batch_route", fake_batch_route)
    monkeypatch.setattr(isochrone, "build_isochrone", lambda *a, **k: None)

    # 显式 delay=5
    isochrone.run_pipeline(origin, polygon, baidu_ak="fake", delay=5)
    assert captured["delay"] == 5

    # 默认 delay=3.0
    captured.clear()
    isochrone.run_pipeline(origin, polygon, baidu_ak="fake")
    assert captured["delay"] == 3.0


def test_main_delay_wiring(monkeypatch, tmp_path):
    poly = tmp_path / "poly.json"
    poly.write_text(json.dumps([
        [120.20, 30.25], [120.23, 30.25], [120.23, 30.28],
        [120.20, 30.28], [120.20, 30.25],
    ]))

    captured = {}

    def fake_run_pipeline(*a, **k):
        captured.update(k)
        return None

    monkeypatch.setattr(isochrone, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", [
        "isochrone.py", "--origin", "120.21", "30.26",
        "--polygon", str(poly), "--delay", "5",
    ])
    isochrone.main()
    assert captured.get("delay") == 5.0


def test_main_rejects_concurrency_arg(monkeypatch, tmp_path):
    poly = tmp_path / "poly.json"
    poly.write_text(json.dumps([
        [120.20, 30.25], [120.23, 30.25], [120.23, 30.28],
        [120.20, 30.28], [120.20, 30.25],
    ]))
    monkeypatch.setattr(sys, "argv", [
        "isochrone.py", "--origin", "120.21", "30.26",
        "--polygon", str(poly), "--concurrency", "2",
    ])
    with pytest.raises(SystemExit):
        isochrone.main()


# --------------------------------------------------------------------------- #
# (P1-2) main: --config 与 --origin/--polygon 同时给出时的告警
# --------------------------------------------------------------------------- #
def test_main_config_and_cli_coords_warning(monkeypatch, tmp_path, caplog):
    import logging

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "origin": [120.21, 30.26],
        "polygon": [[120.20, 30.25], [120.23, 30.25],
                    [120.23, 30.28], [120.20, 30.28], [120.20, 30.25]],
    }))
    monkeypatch.setattr(isochrone, "run_pipeline", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", [
        "isochrone.py", "--config", str(cfg),
        "--origin", "120.21", "30.26", "--polygon", str(tmp_path / "poly.json"),
    ])
    with caplog.at_level(logging.WARNING):
        isochrone.main()
    assert any("命令行坐标被忽略" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# (b) P0-3 修复验证（最关键）：批量算路「中间缺失」时整批标记失败，
#     绝不把错位/错误时长写进 csv（pending 保持，出图 dropna 丢弃）。
# --------------------------------------------------------------------------- #
def test_batch_route_no_misallocation_on_count_mismatch(
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
            # 第一批（2 个 dest）返回「中间缺失」：只回 1 条结果（含一个会被误用的错误值 999）
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

    import pandas as pd
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
