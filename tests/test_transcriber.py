from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types

import pytest

import config
import transcriber
from transcriber import TranscriberError, format_srt


def test_basic_block():
    out = format_srt([{"start": 0.0, "end": 1.5, "text": " Olá "}])
    assert out == "1\n00:00:00,000 --> 00:00:01,500\nOlá\n"


def test_multiple_blocks_separated_by_blank_line():
    out = format_srt([
        {"start": 0, "end": 1, "text": "a"},
        {"start": 1, "end": 2, "text": "b"},
    ])
    assert out == "1\n00:00:00,000 --> 00:00:01,000\na\n\n2\n00:00:01,000 --> 00:00:02,000\nb\n"


def test_hours():
    out = format_srt([{"start": 3661.25, "end": 7322.5, "text": "x"}])
    assert "01:01:01,250 --> 02:02:02,500" in out


def test_rounding_carries():
    out = format_srt([{"start": 59.9996, "end": 60.0004, "text": "x"}])
    assert "00:01:00,000 --> 00:01:00,000" in out


def test_empty_text_skipped_and_renumbered():
    out = format_srt([
        {"start": 0, "end": 1, "text": "   "},
        {"start": 1, "end": 2, "text": "ok"},
        {"start": 2, "end": 3, "text": ""},
        {"start": 3, "end": 4, "text": "fim"},
    ])
    assert out == (
        "1\n00:00:01,000 --> 00:00:02,000\nok\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nfim\n"
    )


def test_empty_list():
    assert format_srt([]) == ""


def test_unicode():
    out = format_srt([{"start": 0, "end": 1, "text": "ação éção — 你好"}])
    assert "ação éção — 你好" in out
    assert out.endswith("\n")


def _fake_faster(calls):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            calls["init"] = (name, device, compute_type)

        def transcribe(self, path, **kwargs):
            calls["transcribe"] = (path, kwargs)

            def gen():
                yield types.SimpleNamespace(start=0.0, end=1.0, text=" Olá ")
                yield types.SimpleNamespace(start=1.0, end=2.0, text="  ")

            return gen(), types.SimpleNamespace(language="pt", language_probability=0.9)

    mod.WhisperModel = WhisperModel
    return mod


def test_faster_load_and_transcribe(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster(calls))
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "WHISPER_COMPUTE_TYPE", "int8")
    monkeypatch.setattr(config, "WHISPER_VAD", True)
    monkeypatch.setattr(config, "WHISPER_LANGUAGE", "pt")
    model = transcriber.load_model("tiny")
    assert calls["init"] == ("tiny", "cpu", "int8")
    out = transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "s" / "a.srt")
    assert out.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:01,000\nOlá\n"
    kw = calls["transcribe"][1]
    assert kw["language"] == "pt" and kw["vad_filter"] is True


def test_faster_language_none_passed_as_none(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster(calls))
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "WHISPER_LANGUAGE", None)
    model = transcriber.load_model()
    transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt")
    assert calls["transcribe"][1]["language"] is None
    transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "b.srt", language="en")
    assert calls["transcribe"][1]["language"] == "en"


def test_faster_lazy_generator_error_wrapped(monkeypatch, tmp_path):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **kw):
            def gen():
                raise RuntimeError("decodificação quebrou")
                yield  # pragma: no cover

            return gen(), types.SimpleNamespace(language="pt", language_probability=1.0)

    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    model = transcriber.load_model()
    with pytest.raises(TranscriberError, match="decodificação quebrou"):
        transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt")


def test_openai_engine(monkeypatch, tmp_path):
    seen: dict = {}
    mod = types.ModuleType("whisper")

    class M:
        def transcribe(self, path, **kw):
            seen.update(kw)
            return {"segments": [{"start": 0, "end": 1, "text": "oi"}]}

    mod.load_model = lambda name: M()
    monkeypatch.setitem(sys.modules, "whisper", mod)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "openai")
    monkeypatch.setattr(config, "WHISPER_LANGUAGE", None)
    model = transcriber.load_model()
    out = transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt")
    assert "oi" in out.read_text(encoding="utf-8")
    assert seen["language"] is None and seen["fp16"] is False


def test_unsupported_engine(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_ENGINE", "xyz")
    with pytest.raises(TranscriberError, match="engine não suportada"):
        transcriber.load_model()


@pytest.mark.parametrize("engine,module,hint", [
    ("faster", "faster_whisper", "pip install faster-whisper"),
    ("openai", "whisper", "pip install openai-whisper"),
])
def test_import_error(monkeypatch, engine, module, hint):
    monkeypatch.setitem(sys.modules, module, None)  # força ImportError
    monkeypatch.setattr(config, "WHISPER_ENGINE", engine)
    with pytest.raises(TranscriberError, match=hint):
        transcriber.load_model()


@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None or shutil.which(config.FFMPEG_BIN) is None,
    reason="faster-whisper ou ffmpeg indisponível",
)
def test_real_faster_whisper_tiny_on_sine(monkeypatch, tmp_path):
    wav = tmp_path / "seno.wav"
    subprocess.run(
        [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    model = transcriber.load_model("tiny")
    out = transcriber.transcribe_to_srt(model, wav, tmp_path / "seno.srt")
    assert out.exists()
