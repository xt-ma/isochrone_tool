"""batch_route 指纹分支回归：
- 指纹不一致 & force=False & 非交互 -> SystemExit 且不改文件
- 指纹不一致 & force=True -> 覆盖重算
- 指纹一致 -> 正常续跑

绝不真实调用百度 API：FakeSession + monkeypatch aiohttp.ClientSession。
"""
import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

import isochrone


def _seed(tmp_csv, polygon, cell, tactics, done_rows, fingerprint):
    pd.DataFrame(done_rows).to_csv(tmp_csv, index=False)
    Path(tmp_csv).with_suffix(".meta.json").write_text(
        json.dumps({
            "fingerprint": fingerprint,
            "cell_deg": cell, "tactics": tactics,
            "origin_bd": None, "polygon_wkt": polygon.wkt,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _normal_side_effect(params):
    ndest = params["destinations"].count("|") + 1
    return {"status": 0, "result": [
        {"duration": {"value": int((10 + i) * 60)}} for i in range(ndest)]}


def test_fingerprint_mismatch_cancel_no_change(
    fake_session, sample_origin, sample_polygon, tmp_csv, monkeypatch
):
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    _seed(tmp_csv, sample_polygon, cell, 11,
          [{"oid": 0, "duration_min": 3.0, "direction": "from",
            "origin_lng": None, "origin_lat": None, "dest_lng": None, "dest_lat": None}],
          fingerprint="deadbeef0000")  # 故意错误指纹
    fake_session(payload={"status": 0, "result": [{"duration": {"value": 600}}]})

    # 非交互：input 直接返回 ""（=取消）
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")

    with pytest.raises(SystemExit):
        asyncio.run(isochrone.batch_route(
            sample_origin, gdf, "fakeak", tactics=11, batch_size=50,
            csv_path=tmp_csv, direction="from", polygon=sample_polygon,
            cell_deg=cell, force=False, delay=0.0))

    # 文件未被改动
    meta = json.loads(Path(tmp_csv).with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["fingerprint"] == "deadbeef0000"
    df = pd.read_csv(tmp_csv)
    assert list(df["oid"]) == [0]


def test_fingerprint_mismatch_force_overwrite(
    fake_session, sample_origin, sample_polygon, tmp_csv
):
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    n = len(gdf)
    _seed(tmp_csv, sample_polygon, cell, 11,
          [{"oid": 0, "duration_min": 3.0, "direction": "from",
            "origin_lng": None, "origin_lat": None, "dest_lng": None, "dest_lat": None}],
          fingerprint="deadbeef0000")
    fake_session(side_effect=_normal_side_effect)

    done = asyncio.run(isochrone.batch_route(
        sample_origin, gdf, "fakeak", tactics=11, batch_size=50,
        csv_path=tmp_csv, direction="from", polygon=sample_polygon,
        cell_deg=cell, origin_bd=(120.21, 30.26), force=True, delay=0.0))

    # 全部重算
    assert set(done.keys()) == set(range(n))
    # 新指纹与当前配置一致
    fp = isochrone._config_fingerprint(sample_origin, sample_polygon, cell, 11)
    meta = json.loads(Path(tmp_csv).with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["fingerprint"] == fp


def test_fingerprint_consistent_resume(
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
    _seed(tmp_csv, sample_polygon, cell, 11, rows, fingerprint=fp)
    fake_session(side_effect=_normal_side_effect)

    # 一致指纹：不抛异常，正常续跑
    done = asyncio.run(isochrone.batch_route(
        sample_origin, gdf, "fakeak", tactics=11, batch_size=50,
        csv_path=tmp_csv, direction="from", polygon=sample_polygon,
        cell_deg=cell, force=False, delay=0.0))
    assert set(done.keys()) == set(range(n))
