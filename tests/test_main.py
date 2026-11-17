"""Testes do orquestrador (main.py) sem rede, ffmpeg ou whisper reais."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

import config
import main
from clipper import ClipSpec
from hype_detector import Spike
from session import Session

URL = "https://www.twitch.tv/canal"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", str(tmp_path / "sessions"))
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, main._HANDLER_TAG, None) == "file":
            root.removeHandler(h)
            h.close()


def _session_with_segment(tmp_path) -> Session:
    session = Session.create(URL, tmp_path)
    seg = session.new_segment()
    seg.path.write_bytes(b"x" * 10)
    return session


def _patch_pipeline(monkeypatch, specs, fail_burn_on=()):
    monkeypatch.setattr(main, "find_spikes", lambda s: [Spike(0, 50.0, 20, 4.0)])
    monkeypatch.setattr(main, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(main, "plan_clips", lambda *a, **k: specs)
    monkeypatch.setattr(main, "cut_clip", lambda src, spec, out: (out.write_bytes(b"c"), out)[1])
    monkeypatch.setattr(main, "transcribe_to_srt", lambda m, clip, srt: (srt.write_text("s"), srt)[1])

    def burn(clip, srt, out):
        if out.name in fail_burn_on or any(k in out.name for k in fail_burn_on):
            raise RuntimeError("burn falhou")
        out.write_bytes(b"f")
        return out

    monkeypatch.setattr(main, "burn_subtitles", burn)


def test_postprocess_second_clip_fails(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    specs = [ClipSpec(0, 10.0, 30.0, 20), ClipSpec(0, 100.0, 30.0, 15)]
    _patch_pipeline(monkeypatch, specs, fail_burn_on=("clip_02",))
    monkeypatch.setattr(main, "load_model", lambda: object())
    finals = main.postprocess(session)
    assert len(finals) == 1
    assert finals[0].name == "clip_01_seg0_00m10s.mp4"
    assert session.status["postprocess"] == "parcial: 1/2 clipes"
    assert session.segments[0].path.exists()


def test_postprocess_no_spikes_does_not_load_model(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [])

    def boom():
        raise AssertionError("modelo não deveria ser carregado")

    monkeypatch.setattr(main, "load_model", boom)
    assert main.postprocess(session) == []
    assert session.status["postprocess"].startswith("ok")


def test_postprocess_model_failure(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])

    def fail():
        raise RuntimeError("sem modelo")

    monkeypatch.setattr(main, "load_model", fail)
    assert main.postprocess(session) == []
    assert session.status["postprocess"].startswith("erro")


def test_postprocess_deletes_raw_when_all_ok(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])
    monkeypatch.setattr(main, "load_model", lambda: object())
    monkeypatch.setattr(config, "DELETE_RAW_AFTER_PROCESS", True)
    assert len(main.postprocess(session)) == 1
    assert not session.segments[0].path.exists()


class _FakeSource:
    pass


class _FakeChat:
    def __init__(self, session, source, stop_event):
        self.stop_event = stop_event
        self.message_count = 0
        self.reconnect_count = 0

    def run(self):
        self.stop_event.wait()


def test_run_session_recorder_finishes_alone(monkeypatch):
    class QuickRecorder:
        def __init__(self, session, stop_event):
            self.stop_event = stop_event

        def run(self):
            self.stop_event.set()

    monkeypatch.setattr(main, "Recorder", QuickRecorder)
    monkeypatch.setattr(main, "ChatListener", _FakeChat)
    monkeypatch.setattr(main, "make_chat_source", lambda url: _FakeSource())
    session = main.run_session(URL)
    assert session.status["record"] == "ok"
    assert session.manifest_path.exists()


def test_run_session_max_duration(monkeypatch):
    class WaitingRecorder:
        def __init__(self, session, stop_event):
            self.stop_event = stop_event

        def run(self):
            self.stop_event.wait()

    monkeypatch.setattr(main, "Recorder", WaitingRecorder)
    monkeypatch.setattr(main, "ChatListener", _FakeChat)
    monkeypatch.setattr(main, "make_chat_source", lambda url: _FakeSource())
    began = time.monotonic()
    session = main.run_session(URL, max_duration=1.0)
    elapsed = time.monotonic() - began
    assert 0.9 <= elapsed < 5.0
    assert session.status["record"] == "ok"


def test_run_session_chat_source_failure_records_video_only(monkeypatch):
    class QuickRecorder:
        def __init__(self, session, stop_event):
            self.stop_event = stop_event

        def run(self):
            self.stop_event.set()

    def bad(url):
        raise RuntimeError("sem chat")

    monkeypatch.setattr(main, "Recorder", QuickRecorder)
    monkeypatch.setattr(main, "make_chat_source", bad)
    assert main.run_session(URL).status["record"] == "ok"


def test_process_missing_session_returns_1(tmp_path):
    assert main.main(["--process", str(tmp_path / "nao_existe")]) == 1


def test_check_prerequisites_invalid_url():
    with pytest.raises(main.PrerequisiteError):
        main.check_prerequisites("https://example.com/canal")


def test_check_prerequisites_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(config, "FFMPEG_BIN", "ffmpeg-inexistente-xyz")
    monkeypatch.setattr(main.sys, "platform", "linux")
    with pytest.raises(main.PrerequisiteError, match="winget"):
        main.check_prerequisites(URL)


def test_main_returns_1_on_prerequisite_error(monkeypatch):
    def fail(url=None):
        raise main.PrerequisiteError("falta algo")

    monkeypatch.setattr(main, "check_prerequisites", fail)
    assert main.main(["--url", URL]) == 1


def test_monitor_loop_once(monkeypatch, tmp_path):
    session = Session.create(URL, tmp_path)
    calls = []
    monkeypatch.setattr(main, "is_live", lambda url: True)
    monkeypatch.setattr(main, "run_session", lambda url, max_duration=None: session)
    monkeypatch.setattr(main, "postprocess", lambda s: calls.append(s) or [])
    main.monitor_loop(URL, once=True, max_duration=1)
    assert calls == [session]


def test_monitor_loop_invalid_url_exits(monkeypatch):
    def bad(url):
        raise main.RecorderError("sem plugin")

    monkeypatch.setattr(main, "is_live", bad)
    with pytest.raises(SystemExit):
        main.monitor_loop(URL, once=True)


def test_setup_logging_idempotent(tmp_path):
    main.setup_logging(tmp_path / "a.log")
    main.setup_logging(tmp_path / "b.log")
    handlers = [
        h for h in logging.getLogger().handlers
        if getattr(h, main._HANDLER_TAG, None) == "file"
    ]
    assert len(handlers) == 1
    assert Path(handlers[0].baseFilename).name == "b.log"
