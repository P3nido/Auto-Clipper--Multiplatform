"""Testes do runtime.py: modo código-fonte e modo congelado (simulado)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import runtime


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Simula o executável do PyInstaller em tmp_path/app."""
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "Auto-Clipper.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "_internal"), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return app


def test_source_mode_basics():
    assert runtime.is_frozen() is False
    assert runtime.app_dir() == Path(runtime.__file__).resolve().parent
    assert runtime.data_dir() == runtime.app_dir()
    assert runtime.default_sessions_dir() == "sessions"
    assert runtime.bundled_tool("ffmpeg") is None


def test_frozen_dirs(frozen, tmp_path):
    assert runtime.is_frozen() is True
    assert runtime.app_dir() == frozen.resolve()
    data = runtime.data_dir()
    assert data == tmp_path / "local" / "Auto-Clipper"
    assert data.is_dir()
    assert runtime.default_sessions_dir() == str(Path.home() / "Videos" / "Auto-Clipper")


def test_data_dir_fallback_without_localappdata(frozen, monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert runtime.data_dir() == tmp_path / "home" / ".auto-clipper"


def test_bundled_tool(frozen):
    assert runtime.bundled_tool("ffmpeg") is None
    (frozen / "ffmpeg").mkdir()
    (frozen / "ffmpeg" / "ffmpeg.exe").write_bytes(b"x")
    found = runtime.bundled_tool("ffmpeg")
    assert found is not None and Path(found).name.lower().startswith("ffmpeg")


def test_streamlink_command_frozen(frozen):
    with pytest.raises(FileNotFoundError):
        runtime.streamlink_command()
    cli = frozen / runtime.CLI_EXE_NAME
    cli.write_bytes(b"")
    assert runtime.streamlink_command() == [str(frozen.resolve() / runtime.CLI_EXE_NAME),
                                            runtime.STREAMLINK_FLAG]


def test_streamlink_command_source(monkeypatch, tmp_path):
    name = "streamlink.exe" if sys.platform == "win32" else "streamlink"
    py = tmp_path / ("python.exe" if sys.platform == "win32" else "python")
    py.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(runtime.shutil, "which", lambda n: None)
    with pytest.raises(FileNotFoundError):
        runtime.streamlink_command()
    monkeypatch.setattr(runtime.shutil, "which", lambda n: "/x/streamlink")
    assert runtime.streamlink_command() == ["/x/streamlink"]
    (tmp_path / name).write_bytes(b"")
    assert runtime.streamlink_command() == [str(tmp_path / name)]


def test_subprocess_flags(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert runtime.subprocess_flags() == 0x08000000
    monkeypatch.setattr(sys, "platform", "linux")
    assert runtime.subprocess_flags() == 0


def test_nvidia_dll_dirs_meipass(frozen):
    for pkg in ("cublas", "cudnn"):
        (frozen / "_internal" / "nvidia" / pkg / "bin").mkdir(parents=True)
    (frozen / "_internal" / "nvidia" / "nvtx" / "include").mkdir(parents=True)
    names = [d.parent.name for d in runtime.nvidia_dll_dirs()]
    assert "cublas" in names and "cudnn" in names and "nvtx" not in names


def test_nvidia_dll_dirs_site_packages(monkeypatch, tmp_path):
    (tmp_path / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert [d.parent.name for d in runtime.nvidia_dll_dirs()] == ["cublas"]
