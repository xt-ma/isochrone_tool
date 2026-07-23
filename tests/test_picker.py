"""picker.py 回归：P1-1 改动——webbrowser.open 失败时打印手动打开提示，
而非抛异常中断。

通过 runpy 以 __main__ 方式真实执行 picker.py 的入口块，
并 monkeypatch webbrowser.open 使其抛异常，验证兜底打印。
"""
import runpy
import sys

import pytest

import picker  # 确保可导入（项目根在 sys.path 上）


def test_picker_build_creates_html(tmp_path):
    out = tmp_path / "picker.html"
    picker.build_picker(out=str(out))
    assert out.exists()
    assert out.stat().st_size > 0


def test_picker_webbrowser_fallback_message(monkeypatch, tmp_path, capsys):
    out = tmp_path / "picker.html"

    # 模拟「无法自动打开浏览器」：webbrowser.open 抛异常
    def _raise(*a, **k):
        raise Exception("no browser available")

    monkeypatch.setattr(picker.webbrowser, "open", _raise)
    monkeypatch.setattr(sys, "argv", ["picker.py", "--out", str(out)])

    runpy.run_path(str(picker.__file__), run_name="__main__")

    captured = capsys.readouterr()
    assert "[提示] 无法自动打开浏览器，请手动打开文件：" in captured.out
    assert str(out) in captured.out
