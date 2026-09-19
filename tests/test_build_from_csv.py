"""build_from_csv 回归：指纹交互（统一确认入口）、方向过滤、旧格式告警与报错。

build_from_csv 是 oid 对齐的第二道防线（与 batch_route 的校验相互独立），
这里逐分支锁住行为；渲染走真实出图（小多边形，离线、快）。
"""
import json
from pathlib import Path

import pandas as pd
import pytest

import isochrone
from tests.conftest import SAMPLE_ORIGIN, SAMPLE_POLYGON

CELL = 0.01


def _n_points():
    return len(isochrone.make_fishnet(SAMPLE_POLYGON, cell_deg=CELL))


def _rows(direction="from"):
    return [
        {"oid": o, "duration_min": 10.0, "direction": direction,
         "origin_lng": None, "origin_lat": None, "dest_lng": None, "dest_lat": None}
        for o in range(_n_points())
    ]


def _seed(tmp_path, rows, fingerprint=None, tactics=11,
          with_meta=True, with_direction=True):
    csv_p = Path(tmp_path) / "durations.csv"
    df = pd.DataFrame(rows)
    if not with_direction:
        df = df.drop(columns=["direction"])
    df.to_csv(csv_p, index=False)
    if with_meta:
        meta = {"cell_deg": CELL, "tactics": tactics, "origin_bd": None,
                "polygon_wkt": SAMPLE_POLYGON.wkt}
        if fingerprint is not None:
            meta["fingerprint"] = fingerprint
        (Path(tmp_path) / "durations.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return str(csv_p)


def _render_kwargs(tmp_path):
    return {"out_html": str(Path(tmp_path) / "iso.html")}


def test_from_csv_happy_path(tmp_path):
    fp = isochrone._config_fingerprint(SAMPLE_ORIGIN, SAMPLE_POLYGON, CELL, 11)
    csv = _seed(tmp_path, _rows(), fingerprint=fp)
    gdf = isochrone.build_from_csv(
        SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL,
        **_render_kwargs(tmp_path))
    assert len(gdf) == _n_points()
    assert (Path(tmp_path) / "iso.html").exists()


def test_from_csv_fingerprint_mismatch_interactive_cancel(tmp_path, monkeypatch):
    """指纹不一致 + 非交互回答空 -> SystemExit，与算路模式行为一致。"""
    csv = _seed(tmp_path, _rows(), fingerprint="deadbeef0000")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    with pytest.raises(SystemExit):
        isochrone.build_from_csv(
            SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL,
            **_render_kwargs(tmp_path))


def test_from_csv_fingerprint_mismatch_interactive_confirm(tmp_path, monkeypatch, caplog):
    import logging
    csv = _seed(tmp_path, _rows(), fingerprint="deadbeef0000")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    with caplog.at_level(logging.WARNING):
        isochrone.build_from_csv(
            SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL,
            **_render_kwargs(tmp_path))
    assert any("指纹不匹配" in r.message for r in caplog.records)
    assert (Path(tmp_path) / "iso.html").exists()


def test_from_csv_fingerprint_mismatch_force(tmp_path):
    csv = _seed(tmp_path, _rows(), fingerprint="deadbeef0000")
    isochrone.build_from_csv(
        SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL, force=True,
        **_render_kwargs(tmp_path))
    assert (Path(tmp_path) / "iso.html").exists()


def test_from_csv_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        isochrone.build_from_csv(
            SAMPLE_ORIGIN, SAMPLE_POLYGON,
            csv_path=str(Path(tmp_path) / "nope.csv"), cell_deg=CELL,
            **_render_kwargs(tmp_path))


def test_from_csv_direction_to_without_to_rows(tmp_path, caplog):
    """方向写反（只有 from 数据却要 to 出图）-> 丢全部点但不崩溃，出仅底图。"""
    import logging
    fp = isochrone._config_fingerprint(SAMPLE_ORIGIN, SAMPLE_POLYGON, CELL, 11)
    csv = _seed(tmp_path, _rows(direction="from"), fingerprint=fp)
    with caplog.at_level(logging.ERROR):
        gdf = isochrone.build_from_csv(
            SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL,
            direction="to", **_render_kwargs(tmp_path))
    assert len(gdf) == 0
    assert any("没有任何有效采样点" in r.message for r in caplog.records)
    assert (Path(tmp_path) / "iso.html").exists()


def test_from_csv_old_format_no_direction_column_to_raises(tmp_path):
    """旧格式 csv（无 direction 列）按 to 出图 -> 明确报错而非静默当 from。"""
    csv = _seed(tmp_path, _rows(), with_meta=False, with_direction=False)
    with pytest.raises(ValueError):
        isochrone.build_from_csv(
            SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL,
            direction="to", **_render_kwargs(tmp_path))


def test_from_csv_no_meta_warns(tmp_path, caplog):
    import logging
    csv = _seed(tmp_path, _rows(), with_meta=False)
    with caplog.at_level(logging.WARNING):
        isochrone.build_from_csv(
            SAMPLE_ORIGIN, SAMPLE_POLYGON, csv_path=csv, cell_deg=CELL,
            **_render_kwargs(tmp_path))
    assert any("无指纹信息" in r.message for r in caplog.records)
