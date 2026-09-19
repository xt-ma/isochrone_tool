"""CLI 接线回归：参数透传、未知参数拒绝、config 优先级与冲突告警、浏览器打开。

全部通过 monkeypatch 隔离：不触网、不真正打开浏览器、不真正算路。
"""
import json
import sys

import pytest
from shapely.geometry import Polygon

import isochrone


# --------------------------------------------------------------------------- #
# pandas 导入冒烟（requirements.txt 声明了 pandas>=2.0）
# --------------------------------------------------------------------------- #
def test_pandas_import():
    import pandas as pd  # noqa: F401
    assert hasattr(isochrone, "make_fishnet")


def _square_polygon():
    return Polygon([(120.20, 30.25), (120.23, 30.25),
                    (120.23, 30.28), (120.20, 30.28)])


# --------------------------------------------------------------------------- #
# --delay 接线：run_pipeline 与 main 均透传
# --------------------------------------------------------------------------- #
def test_run_pipeline_delay_passthrough(monkeypatch):
    origin = (30.265, 120.215)
    polygon = _square_polygon()

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
    ]), encoding="utf-8")

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


# --------------------------------------------------------------------------- #
# --export-geojson 接线：CLI 开关与 config 字段都能透传到 run_pipeline
# --------------------------------------------------------------------------- #
def _write_config(tmp_path, extra=None):
    cfg = {
        "origin": [120.21, 30.26],
        "polygon": [[120.20, 30.25], [120.23, 30.25], [120.23, 30.28],
                    [120.20, 30.28], [120.20, 30.25]],
    }
    cfg.update(extra or {})
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_main_export_geojson_flag(monkeypatch, tmp_path):
    cfg = _write_config(tmp_path)
    captured = {}
    monkeypatch.setattr(isochrone, "run_pipeline",
                        lambda *a, **k: captured.update(k))
    monkeypatch.setattr(sys, "argv",
                        ["isochrone.py", "--config", cfg, "--export-geojson"])
    isochrone.main()
    assert captured.get("export_geojson") is True


def test_main_export_geojson_from_config_field(monkeypatch, tmp_path):
    cfg = _write_config(tmp_path, extra={"export_geojson": True})
    captured = {}
    monkeypatch.setattr(isochrone, "run_pipeline",
                        lambda *a, **k: captured.update(k))
    monkeypatch.setattr(sys, "argv", ["isochrone.py", "--config", cfg])
    isochrone.main()
    assert captured.get("export_geojson") is True


# --------------------------------------------------------------------------- #
# --open：出图完成后用浏览器打开结果
# --------------------------------------------------------------------------- #
def test_main_open_opens_browser(monkeypatch, tmp_path):
    poly = tmp_path / "poly.json"
    poly.write_text(json.dumps([
        [120.20, 30.25], [120.23, 30.25], [120.23, 30.28],
        [120.20, 30.28], [120.20, 30.25],
    ]), encoding="utf-8")
    out = tmp_path / "demo.html"
    opened = []
    monkeypatch.setattr(isochrone.webbrowser, "open",
                        lambda uri: opened.append(uri) or True)
    monkeypatch.setattr(sys, "argv", [
        "isochrone.py", "--origin", "120.21", "30.26",
        "--polygon", str(poly), "--demo", "--open", "--out", str(out),
    ])
    isochrone.main()
    assert out.exists()
    assert len(opened) == 1
    assert "demo.html" in opened[0]


def test_main_without_open_does_not_open_browser(monkeypatch, tmp_path):
    poly = tmp_path / "poly.json"
    poly.write_text(json.dumps([
        [120.20, 30.25], [120.23, 30.25], [120.23, 30.28],
        [120.20, 30.28], [120.20, 30.25],
    ]), encoding="utf-8")
    opened = []
    monkeypatch.setattr(isochrone.webbrowser, "open",
                        lambda uri: opened.append(uri) or True)
    monkeypatch.setattr(sys, "argv", [
        "isochrone.py", "--origin", "120.21", "30.26",
        "--polygon", str(poly), "--demo", "--out", str(tmp_path / "demo.html"),
    ])
    isochrone.main()
    assert opened == []


# --------------------------------------------------------------------------- #
# 拒绝已移除的 --concurrency 参数（当前为串行 by design）
# --------------------------------------------------------------------------- #
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
# main: --config 与 --origin/--polygon 同时给出时告警（命令行坐标被忽略）
# --------------------------------------------------------------------------- #
def test_main_config_and_cli_coords_warning(monkeypatch, tmp_path, caplog):
    import logging

    cfg = _write_config(tmp_path)
    monkeypatch.setattr(isochrone, "run_pipeline", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", [
        "isochrone.py", "--config", cfg,
        "--origin", "120.21", "30.26", "--polygon", str(tmp_path / "poly.json"),
    ])
    with caplog.at_level(logging.WARNING):
        isochrone.main()
    assert any("命令行坐标被忽略" in r.message for r in caplog.records)
