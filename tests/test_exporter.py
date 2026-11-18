"""Testes do exporter (ADR-011): sem rede, tudo em tmp_path."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import config
import exporter
from exporter import ExportError


@pytest.fixture
def no_drive(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(exporter, "_drive_letters", lambda: [])


def _clip(folder: Path, name: str, data: bytes = b"video") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(data)
    return p


def test_resolve_auto_sem_drive(no_drive):
    assert exporter.resolve_export_dir("auto") is None
    assert exporter.resolve_export_dir("") is None
    assert exporter.resolve_export_dir(None) is None


def test_resolve_auto_com_drive(monkeypatch, tmp_path):
    root = tmp_path / "Meu Drive"
    root.mkdir()
    monkeypatch.setattr(exporter, "_candidate_roots", lambda: [tmp_path / "x", root])
    assert exporter.resolve_export_dir("auto") == root / config.EXPORT_SUBFOLDER


def test_resolve_custom_sem_subpasta(tmp_path):
    assert exporter.resolve_export_dir(str(tmp_path / "meu")) == tmp_path / "meu"


def test_destination_for(tmp_path):
    base = tmp_path / "base"
    assert exporter.destination_for(base, tmp_path / "sess1", []) == base / "sess1"
    clip = tmp_path / "sess2" / "final" / "a.mp4"
    assert exporter.destination_for(base, None, [clip]) == base / "sess2"
    assert exporter.destination_for(base, None, []) == base


def test_is_already_exported(tmp_path):
    clip = _clip(tmp_path / "src", "a.mp4", b"12345")
    dest = tmp_path / "dest"
    assert not exporter.is_already_exported(clip, dest)
    _clip(dest, "a.mp4", b"123")
    assert not exporter.is_already_exported(clip, dest)
    _clip(dest, "a.mp4", b"12345")
    assert exporter.is_already_exported(clip, dest)


def test_export_copia_e_cria_pastas(tmp_path):
    clip = _clip(tmp_path / "src", "a.mp4", b"abc")
    dest = tmp_path / "x" / "y" / "z"
    res = exporter.export_clips([clip], dest)
    assert (dest / "a.mp4").read_bytes() == b"abc"
    assert res.copied == (dest / "a.mp4",)
    assert res.skipped == ()
    assert not list(dest.glob("*.part"))


def test_export_pula_igual(tmp_path):
    clip = _clip(tmp_path / "src", "a.mp4", b"abc")
    dest = tmp_path / "dest"
    exporter.export_clips([clip], dest)
    res = exporter.export_clips([clip], dest)
    assert res.copied == ()
    assert res.skipped == (clip,)


def test_export_sufixo_para_tamanho_diferente(tmp_path):
    clip = _clip(tmp_path / "src", "a.mp4", b"abcdef")
    dest = tmp_path / "dest"
    _clip(dest, "a.mp4", b"x")
    res = exporter.export_clips([clip], dest)
    assert res.copied == (dest / "a (2).mp4",)
    assert (dest / "a.mp4").read_bytes() == b"x"
    assert (dest / "a (2).mp4").read_bytes() == b"abcdef"


def test_export_progresso_e_stop(tmp_path):
    clips = [_clip(tmp_path / "src", f"{i}.mp4", b"a" * i) for i in range(1, 4)]
    calls: list[tuple[int, int]] = []
    exporter.export_clips(clips, tmp_path / "d", on_progress=lambda i, t: calls.append((i, t)))
    assert calls == [(1, 3), (2, 3), (3, 3)]

    stop = threading.Event()

    def progress(i: int, t: int) -> None:
        stop.set()

    res = exporter.export_clips(clips, tmp_path / "d2", on_progress=progress, stop_event=stop)
    assert len(res.copied) == 1


def test_export_origem_inexistente(tmp_path):
    dest = tmp_path / "dest"
    with pytest.raises(ExportError):
        exporter.export_clips([tmp_path / "nao.mp4"], dest)
    assert not list(dest.glob("*.part")) if dest.exists() else True


def test_detect_unidade_simulada(monkeypatch, tmp_path):
    drive = tmp_path / "G"
    (drive / "Meu Drive").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(exporter, "_drive_letters", lambda: [drive])
    assert exporter.detect_drive_folder() == drive / "Meu Drive"


@pytest.mark.parametrize("rel", [("Google Drive", "My Drive"), ("Google Drive",), ("Meu Drive",)])
def test_detect_home(monkeypatch, tmp_path, rel):
    home = tmp_path / "home"
    target = home.joinpath(*rel)
    target.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(exporter, "_drive_letters", lambda: [])
    assert exporter.detect_drive_folder() == target


def test_detect_nada(no_drive):
    assert exporter.detect_drive_folder() is None
