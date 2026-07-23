"""batch_route 断点续跑回归：只补 pending、已算点不被重复请求。

绝不真实调用百度 API：FakeSession 注入预设 JSON，monkeypatch aiohttp.ClientSession。
"""
import asyncio
import json
from pathlib import Path

import pandas as pd

import isochrone


def _write_csv_and_meta(csv_path, polygon, cell, tactics, done_rows, fingerprint):
    pd.DataFrame(done_rows).to_csv(csv_path, index=False)
    meta = {
        "fingerprint": fingerprint,
        "cell_deg": cell, "tactics": tactics,
        "origin_bd": None, "polygon_wkt": polygon.wkt,
    }
    Path(csv_path).with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def test_resume_only_requests_pending(fake_session, sample_origin, sample_polygon, tmp_csv):
    cell = 0.01
    gdf = isochrone.make_fishnet(sample_polygon, cell_deg=cell)
    n = len(gdf)
    done_oids = list(range(n // 2))  # 前半已完成
    rows = [{
        "oid": o, "duration_min": 3.0, "direction": "from",
        "origin_lng": None, "origin_lat": None,
        "dest_lng": round(float(gdf.iloc[o].lon), 6),
        "dest_lat": round(float(gdf.iloc[o].lat), 6),
    } for o in done_oids]
    fp = isochrone._config_fingerprint(sample_origin, sample_polygon, cell, 11)
    _write_csv_and_meta(tmp_csv, sample_polygon, cell, 11, rows, fingerprint=fp)

    def side_effect(params):
        ndest = params["destinations"].count("|") + 1
        return {"status": 0, "result": [
            {"duration": {"value": int((10 + i) * 60)}} for i in range(ndest)]}

    fs = fake_session(side_effect=side_effect)
    done = asyncio.run(isochrone.batch_route(
        sample_origin, gdf, "fakeak", tactics=11, batch_size=50,
        csv_path=tmp_csv, direction="from", polygon=sample_polygon,
        cell_deg=cell, origin_bd=(120.21, 30.26), force=False, delay=0.0))

    # 所有点都算出来了（已算 + 续算）
    assert set(done.keys()) == set(range(n))

    # 已算点的 WGS84 坐标不应出现在任何一次请求的 destinations 中
    done_coords = {
        (round(float(gdf.iloc[o].lat), 6), round(float(gdf.iloc[o].lon), 6))
        for o in done_oids
    }
    for params in fs.calls:
        for (la, lo) in done_coords:
            assert f"{la},{lo}" not in params["destinations"]

    # pending 合为 1 批（batch_size=50），故仅 1 次请求
    assert len(fs.calls) == 1
