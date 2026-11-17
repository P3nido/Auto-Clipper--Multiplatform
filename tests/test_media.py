"""Integração com ffmpeg real (pula se ffmpeg/ffprobe não estiverem no PATH)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import clipper
import config
from clipper import ClipperError, ClipSpec

pytestmark = pytest.mark.skipif(
    shutil.which(config.FFMPEG_BIN) is None or shutil.which(config.FFPROBE_BIN) is None,
    reason="ffmpeg/ffprobe indisponíveis",
)

SRT = "1\n00:00:00,500 --> 00:00:02,000\nOlá mundo\n"


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("media") / "com espaço"
    d.mkdir()
    out = d / "sample.ts"
    subprocess.run(
        [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6",
         "-c:v", "libx264", "-c:a", "aac", "-f", "mpegts", str(out)],
        check=True,
    )
    return out


@pytest.fixture(scope="module")
def clip(sample) -> Path:
    spec = ClipSpec(0, 1.5, 2.5, 10)
    return clipper.cut_clip(sample, spec, sample.parent / "out dir" / "clip.mp4")


def test_probe_duration(sample):
    assert clipper.probe_duration(sample) == pytest.approx(6.0, abs=0.3)


def test_cut_clip_duration(clip):
    assert clip.exists()
    assert clipper.probe_duration(clip) == pytest.approx(2.5, abs=0.15)


def test_probe_invalid_file(tmp_path):
    bad = tmp_path / "x.ts"
    bad.write_text("nada")
    with pytest.raises(ClipperError):
        clipper.probe_duration(bad)


def _srt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SRT, encoding="utf-8")
    return path


def _probe_audio(path: Path) -> dict[str, str]:
    out = subprocess.run(
        [config.FFPROBE_BIN, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    return {"sample_rate": out[0], "channels": out[1]}


def test_render_clip_precise_cut(sample, tmp_path):
    out = clipper.render_clip(sample, ClipSpec(0, 1.5, 2.5, 10), tmp_path / "r.mp4")
    assert clipper.probe_duration(out) == pytest.approx(2.5, abs=0.15)


def test_render_clip_with_subtitles(sample, tmp_path):
    srt = _srt(tmp_path / "com espaço" / "a.srt")
    out = clipper.render_clip(sample, ClipSpec(0, 1.5, 2.5, 10), tmp_path / "final.mp4", srt)
    assert clipper.probe_duration(out) == pytest.approx(2.5, abs=0.15)


def test_render_clip_srt_name_with_quote_and_space(sample, tmp_path):
    d = tmp_path / "com espaço"
    srt = _srt(d / "it's a,test [1].srt")
    out = clipper.render_clip(sample, ClipSpec(0, 1.5, 2.5, 10), d / "final.mp4", srt)
    assert clipper.probe_duration(out) == pytest.approx(2.5, abs=0.15)
    assert [p.name for p in d.iterdir() if p.name.startswith("_burn_")] == []


def test_render_clip_empty_srt_no_filter(sample, tmp_path):
    srt = tmp_path / "e.srt"
    srt.write_text("  \n", encoding="utf-8")
    out = clipper.render_clip(sample, ClipSpec(0, 1.5, 2.5, 10), tmp_path / "e.mp4", srt)
    assert clipper.probe_duration(out) == pytest.approx(2.5, abs=0.15)


def test_extract_audio_mono_16k(sample, tmp_path):
    wav = clipper.extract_audio(sample, ClipSpec(0, 1.0, 3.0, 10), tmp_path / "sub dir" / "a.wav")
    assert wav.exists()
    assert clipper.probe_duration(wav) == pytest.approx(3.0, abs=0.15)
    assert _probe_audio(wav) == {"sample_rate": "16000", "channels": "1"}


def test_extract_audio_without_audio_track(tmp_path):
    silent = tmp_path / "mudo.mp4"
    subprocess.run(
        [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10", "-t", "3",
         "-c:v", "libx264", str(silent)],
        check=True,
    )
    with pytest.raises(ClipperError, match="sem trilha de áudio"):
        clipper.extract_audio(silent, ClipSpec(0, 0, 2, 1), tmp_path / "x.wav")


def test_ffmpeg_missing(monkeypatch, tmp_path, sample):
    srt = _srt(tmp_path / "a.srt")

    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ClipperError, match="FFmpeg não encontrado"):
        clipper.cut_clip(sample, ClipSpec(0, 0, 1, 1), tmp_path / "o.mp4")
    with pytest.raises(ClipperError, match="FFmpeg não encontrado"):
        clipper.render_clip(sample, ClipSpec(0, 0, 1, 1), tmp_path / "o.mp4", srt)
    with pytest.raises(ClipperError, match="FFmpeg não encontrado"):
        clipper.extract_audio(sample, ClipSpec(0, 0, 1, 1), tmp_path / "o.wav")
    with pytest.raises(ClipperError):
        clipper.probe_duration(sample)

