"""Integração com ffmpeg real (pula se ffmpeg/ffprobe não estiverem no PATH)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import clipper
import config
import transcriber
from clipper import ClipperError, ClipSpec
from transcriber import TranscriberError

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


def test_burn_subtitles(clip, tmp_path):
    srt = tmp_path / "com espaço" / "a.srt"
    srt.parent.mkdir()
    srt.write_text(SRT, encoding="utf-8")
    out = transcriber.burn_subtitles(clip, srt, tmp_path / "final.mp4")
    assert clipper.probe_duration(out) == pytest.approx(clipper.probe_duration(clip), abs=0.2)


def test_burn_srt_name_with_quote_and_space(clip, tmp_path):
    d = tmp_path / "com espaço"
    d.mkdir()
    srt = d / "it's a,test [1].srt"
    srt.write_text(SRT, encoding="utf-8")
    out = transcriber.burn_subtitles(clip, srt, d / "final.mp4")
    assert clipper.probe_duration(out) == pytest.approx(clipper.probe_duration(clip), abs=0.2)
    assert [p.name for p in d.iterdir() if p.name.startswith("_burn_")] == []


def test_burn_empty_srt_copies(clip, tmp_path):
    srt = tmp_path / "e.srt"
    srt.write_text("  \n", encoding="utf-8")
    out = transcriber.burn_subtitles(clip, srt, tmp_path / "copy.mp4")
    assert out.read_bytes() == clip.read_bytes()


def test_ffmpeg_missing(monkeypatch, tmp_path, clip):
    srt = tmp_path / "a.srt"
    srt.write_text(SRT, encoding="utf-8")

    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ClipperError, match="FFmpeg não encontrado"):
        clipper.cut_clip(clip, ClipSpec(0, 0, 1, 1), tmp_path / "o.mp4")
    with pytest.raises(ClipperError):
        clipper.probe_duration(clip)
    with pytest.raises(TranscriberError, match="FFmpeg não encontrado"):
        transcriber.burn_subtitles(clip, srt, tmp_path / "f.mp4")
