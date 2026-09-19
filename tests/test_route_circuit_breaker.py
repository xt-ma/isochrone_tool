"""批量算路熔断回归：连续整批失败自动中止、部分成功重置计数、已有数据不受影响。

绝不真实调用百度 API：FakeSession + monkeypatch aiohttp.ClientSession。
"""
import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

import isochrone


def _all_fail_payload():
    return {"status": 210, "message": "当前配额已用尽"}


def _normal_side_effect(params):
    ndest = params["destinations"].count("|") + 1
    return {"status": 0, "result": [
        {"duration": {"value": int((10 + i) * 60)}} for i in range(ndest)]}


def test_breaker_aborts_after_consecutive_failures(
    fake_session, sample_origin, sample_polygon, tmp_csv
):
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    fs = fake_session(payload=_all_fail_payload())

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(isochrone.batch_route(
            sample_origin, gdf, "fakeak", batch_size=2,
            csv_path=tmp_csv, direction="from", polygon=sample_polygon,
            cell_deg=cell, origin_bd=(120.21, 30.26), force=False,
            delay=0.0, max_retry=1, max_failed_batches=3))

    # 恰好 3 批失败即熔断：不再空跑剩余批次
    assert len(fs.calls) == 3
    assert "已熔断中止" in str(ei.value)
    # 从未成功过 -> 不写空 csv（避免覆盖好数据 / 留下坏文件）
    assert not Path(tmp_csv).exists()


def test_breaker_resets_on_success(fake_session, sample_origin, sample_polygon, tmp_csv):
    """连续失败后一旦有批成功，计数重置；失败的批次留待下次续算。"""
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    n = len(gdf)
    state = {"n": 0}

    def side_effect(params):
        state["n"] += 1
        if state["n"] <= 2:
            return _all_fail_payload()
        return _normal_side_effect(params)

    fake_session(side_effect=side_effect)
    done = asyncio.run(isochrone.batch_route(
        sample_origin, gdf, "fakeak", batch_size=2,
        csv_path=tmp_csv, direction="from", polygon=sample_polygon,
        cell_deg=cell, origin_bd=(120.21, 30.26), force=False,
        delay=0.0, max_retry=1, max_failed_batches=3))

    # 前两批（oid 0-3）失败，第三批起成功 -> 熔断计数被重置，不中止
    assert set(done.keys()) == set(range(4, n))
    assert Path(tmp_csv).exists()


def test_breaker_preserves_existing_csv(
    fake_session, sample_origin, sample_polygon, tmp_csv
):
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    n = len(gdf)
    done_oids = list(range(n // 2))
    rows = [{"oid": o, "duration_min": 3.0, "direction": "from",
             "origin_lng": None, "origin_lat": None,
             "dest_lng": None, "dest_lat": None} for o in done_oids]
    fp = isochrone._config_fingerprint(sample_origin, sample_polygon, cell, 11)
    pd.DataFrame(rows).to_csv(tmp_csv, index=False)
    Path(tmp_csv).with_suffix(".meta.json").write_text(json.dumps(
        {"fingerprint": fp, "cell_deg": cell, "tactics": 11,
         "origin_bd": None, "polygon_wkt": sample_polygon.wkt},
        ensure_ascii=False), encoding="utf-8")

    fake_session(payload=_all_fail_payload())
    with pytest.raises(RuntimeError):
        asyncio.run(isochrone.batch_route(
            sample_origin, gdf, "fakeak", batch_size=2,
            csv_path=tmp_csv, direction="from", polygon=sample_polygon,
            cell_deg=cell, origin_bd=(120.21, 30.26), force=False,
            delay=0.0, max_retry=1, max_failed_batches=3))

    # 熔断不破坏已有数据：已算好的行原样保留
    df = pd.read_csv(tmp_csv)
    assert set(df["oid"].tolist()) == set(done_oids)
    assert (df["duration_min"] == 3.0).all()
