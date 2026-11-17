from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types

import pytest

import config
import hardware
import transcriber
from transcriber import TranscriberError, format_srt, split_into_cues


def _profile(device="cpu", compute="int8", model="small"):
    return hardware.HardwareProfile("cpu", None, device, compute, model)


@pytest.fixture(autouse=True)
def _cpu_profile(monkeypatch):
    """Isola os testes do hardware real da máquina."""
    monkeypatch.setattr(hardware, "detect", lambda force=False: _profile())
    monkeypatch.setattr(hardware, "prepare_cuda_dlls", lambda: None)


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


def _words(text, t0=0.0, step=0.4, gap=0.0):
    out, t = [], t0
    for w in text.split():
        out.append({"start": t, "end": t + step, "word": " " + w})
        t += step + gap
    return out


def _cues(words, max_chars=40, max_duration=3.0):
    return split_into_cues(words, max_chars=max_chars, max_duration=max_duration)


def test_cues_empty():
    assert _cues([]) == []


def test_cues_long_sentence_without_punctuation():
    text = " ".join(["palavra"] * 25)
    cues = _cues(_words(text))
    assert len(cues) > 3
    assert all(len(c["text"]) <= 40 for c in cues)
    assert all(c["end"] - c["start"] <= 3.0 + 1e-9 for c in cues)
    assert " ".join(c["text"] for c in cues) == text


def test_cues_sentence_end_breaks():
    cues = _cues(_words("oi mano. tudo bem? sim senhor"))
    assert [c["text"] for c in cues] == ["oi mano.", "tudo bem?", "sim senhor"]


def test_cues_comma_breaks_only_when_60_percent_full():
    short = _cues(_words("oi, mano beleza"))
    assert [c["text"] for c in short] == ["oi, mano beleza"]
    long = _cues(_words("aquele cara jogou muito, mano beleza"), max_chars=40)  # 24 chars >= 24
    assert long[0]["text"] == "aquele cara jogou muito,"
    assert long[1]["text"] == "mano beleza"


def test_cues_pause_breaks():
    words = _words("um dois") + _words("tres quatro", t0=5.0)
    assert [c["text"] for c in _cues(words)] == ["um dois", "tres quatro"]


def test_cues_giant_word_alone():
    giant = "a" * 60
    cues = _cues(_words(f"oi {giant} fim"))
    assert [c["text"] for c in cues] == ["oi", giant, "fim"]


def test_cues_orphan_merged():
    cues = _cues(_words("isso foi muito bom, né?"), max_chars=15)
    assert cues[-1]["text"].endswith("né?")
    assert len(cues[-1]["text"].split()) > 1
    # sem tolerância possível, o órfão fica sozinho
    far = _cues(_words("a" * 20 + " " + "né?"), max_chars=15)
    assert far[-1]["text"] == "né?"


def test_cues_no_overlap_and_positive_duration():
    words = [
        {"start": 0.0, "end": 0.0, "word": "a"},
        {"start": 0.1, "end": 0.2, "word": "b."},
        {"start": 0.25, "end": 0.3, "word": "c."},
    ]
    cues = _cues(words)
    for c, n in zip(cues, cues[1:]):
        assert c["end"] <= n["start"]
    assert all(c["end"] > c["start"] for c in cues)


def test_cues_invalid_times_ignored():
    words = [
        {"start": None, "end": 1.0, "word": "x"},
        {"start": float("nan"), "end": 1.0, "word": "y"},
        {"start": 0.0, "end": 0.5, "word": " ok"},
    ]
    assert [c["text"] for c in _cues(words)] == ["ok"]


def _faster_with_words():
    mod = types.ModuleType("faster_whisper")
    text = " ".join(["palavra"] * 12)

    class WhisperModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **kw):
            words = [types.SimpleNamespace(start=i * 0.5, end=i * 0.5 + 0.4, word=" " + w)
                     for i, w in enumerate(text.split())]

            def gen():
                yield types.SimpleNamespace(start=0.0, end=6.0, text=" " + text, words=words)

            return gen(), types.SimpleNamespace(language="pt", language_probability=1.0)

    mod.WhisperModel = WhisperModel
    return mod


def test_faster_short_cues(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "faster_whisper", _faster_with_words())
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "SUBTITLE_MAX_CHARS", 40)
    model = transcriber.load_model()
    srt = transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt").read_text(encoding="utf-8")
    assert srt.count(" --> ") > 1
    assert all(len(line) <= 40 for line in srt.splitlines() if "palavra" in line)


def test_faster_old_behavior_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "faster_whisper", _faster_with_words())
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "SUBTITLE_MAX_CHARS", 0)
    model = transcriber.load_model()
    srt = transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt").read_text(encoding="utf-8")
    assert srt.count(" --> ") == 1


def test_openai_short_cues(monkeypatch, tmp_path):
    mod = types.ModuleType("whisper")
    text = " ".join(["palavra"] * 12)

    class M:
        def transcribe(self, path, **kw):
            assert kw["word_timestamps"] is True
            words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "word": " " + w}
                     for i, w in enumerate(text.split())]
            return {"segments": [{"start": 0, "end": 6, "text": text, "words": words}]}

    mod.load_model = lambda name: M()
    monkeypatch.setitem(sys.modules, "whisper", mod)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "openai")
    monkeypatch.setattr(config, "SUBTITLE_MAX_CHARS", 40)
    model = transcriber.load_model()
    srt = transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt").read_text(encoding="utf-8")
    assert srt.count(" --> ") > 1


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


def test_orphan_in_the_middle_is_merged_into_previous():
    from transcriber import split_into_cues

    def w(s, e, t):
        return {"start": s, "end": e, "word": t}

    words = [w(0.0, 0.4, "E"), w(0.4, 0.8, "estou"), w(0.8, 1.2, "esperando"),
             w(1.2, 1.5, "até"), w(1.5, 1.8, "hoje"), w(1.8, 2.0, "o"), w(2.0, 2.4, "50%,"),
             w(2.4, 2.7, "né?"), w(2.8, 3.2, "O"), w(3.2, 3.6, "que"), w(3.6, 4.0, "eu"),
             w(4.0, 4.4, "falei.")]
    cues = split_into_cues(words, max_chars=30, max_duration=3.0)
    texts = [c["text"] for c in cues]
    assert "né?" not in texts
    assert any(t.endswith("50%, né?") for t in texts)
    assert texts[-1] == "O que eu falei."


def test_cuda_load_failure_falls_back_to_cpu(monkeypatch):
    calls: list = []
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            calls.append((name, device, compute_type))
            if device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found")

    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "WHISPER_MODEL", "auto")
    monkeypatch.setattr(hardware, "detect", lambda force=False: _profile("cuda", "float16", "large-v3-turbo"))
    model = transcriber.load_model()
    assert calls == [("large-v3-turbo", "cuda", "float16"), ("small", "cpu", "int8")]
    assert (model.device, model.compute, model.name) == ("cpu", "int8", "small")


def test_cuda_loaded_with_profile_values(monkeypatch):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster(calls))
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "WHISPER_MODEL", "auto")
    monkeypatch.setattr(hardware, "detect", lambda force=False: _profile("cuda", "float16", "large-v3-turbo"))
    model = transcriber.load_model()
    assert calls["init"] == ("large-v3-turbo", "cuda", "float16")
    assert model.device == "cuda"


def test_first_cuda_transcription_failure_falls_back_to_cpu(monkeypatch, tmp_path):
    inits: list = []
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            self.device = device
            inits.append((name, device, compute_type))

        def transcribe(self, path, **kw):
            if self.device == "cuda":
                raise RuntimeError("CUBLAS_STATUS_NOT_SUPPORTED")
            return iter([types.SimpleNamespace(start=0.0, end=1.0, text="Oi")]), types.SimpleNamespace(
                language="pt", language_probability=1.0)

    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(config, "WHISPER_MODEL", "auto")
    monkeypatch.setattr(hardware, "detect", lambda force=False: _profile("cuda", "float16", "large-v3-turbo"))
    model = transcriber.load_model()
    out = transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "a.srt")
    assert "Oi" in out.read_text(encoding="utf-8")
    assert model.device == "cpu"
    assert inits[-1] == ("small", "cpu", "int8")
    # a segunda transcrição já usa CPU, sem recarregar
    transcriber.transcribe_to_srt(model, tmp_path / "a.wav", tmp_path / "b.srt")
    assert len(inits) == 2
