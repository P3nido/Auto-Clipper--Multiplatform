import subprocess
import threading
import time

import pytest

import config
import recorder
from recorder import Recorder, RecorderError, remux_to_mp4
from session import Session


@pytest.mark.parametrize(
    "line,expected",
    [
        ("[plugins.twitch][info] Detected advertisement break of 30 seconds", 30.0),
        ("Detected advertisement break of 1 second", 1.0),
        ("[cli][info] Opening stream: 1080p60 (hls)", None),
        ("", None),
    ],
)
def test_parse_ad_break(line, expected):
    assert Recorder._parse_ad_break(line) == expected


def test_remux_command(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    ts = tmp_path / "live_00.ts"
    out = remux_to_mp4(ts)
    assert out == tmp_path / "live_00.mp4"
    assert calls[0] == [
        config.FFMPEG_BIN, "-y", "-i", str(ts),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ]


def test_remux_ffmpeg_missing(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    with pytest.raises(RecorderError, match="FFmpeg não encontrado"):
        remux_to_mp4(tmp_path / "x.ts")


def test_remux_failure_includes_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recorder.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    with pytest.raises(RecorderError, match="boom"):
        remux_to_mp4(tmp_path / "x.ts")


# --- Recorder.run com _record_segment / is_live mockados ---------------------

@pytest.fixture
def session(tmp_path):
    return Session.create("https://www.twitch.tv/teste", tmp_path)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(config, "RECORDING_RESUME_GRACE_S", 0.6)
    monkeypatch.setattr(recorder, "_RESUME_CHECK_INTERVAL_S", 0.05)


def _fake_record(monkeypatch, record_calls):
    def fake(self, segment):
        record_calls.append(segment.index)
        segment.wall_start = 1.0
        segment.wall_end = 2.0
        return 0

    monkeypatch.setattr(Recorder, "_record_segment", fake)


def test_run_live_ends(monkeypatch, session, fast):
    calls = []
    _fake_record(monkeypatch, calls)
    monkeypatch.setattr(recorder, "is_live", lambda url: False)
    stop = threading.Event()
    t0 = time.monotonic()
    Recorder(session, stop).run()
    assert calls == [0]
    assert len(session.segments) == 1
    assert stop.is_set()
    assert time.monotonic() - t0 < 2


def test_run_resumes_within_grace(monkeypatch, session, fast):
    calls = []
    _fake_record(monkeypatch, calls)
    answers = iter([False, False, True])  # volta ao vivo na 3a checagem
    monkeypatch.setattr(
        recorder, "is_live", lambda url: next(answers, False)
    )
    stop = threading.Event()
    Recorder(session, stop).run()
    assert calls == [0, 1]
    assert len(session.segments) == 2
    assert stop.is_set()


def test_run_external_stop_skips_grace(monkeypatch, session):
    monkeypatch.setattr(config, "RECORDING_RESUME_GRACE_S", 30)
    monkeypatch.setattr(recorder, "_RESUME_CHECK_INTERVAL_S", 10)
    stop = threading.Event()
    calls = []

    def fake(self, segment):
        calls.append(segment.index)
        self.stop_event.set()  # simula Ctrl+C durante a gravação
        return 0

    monkeypatch.setattr(Recorder, "_record_segment", fake)
    monkeypatch.setattr(recorder, "is_live", lambda url: pytest.fail("não deveria checar"))
    t0 = time.monotonic()
    Recorder(session, stop).run()
    assert calls == [0]
    assert time.monotonic() - t0 < 2


def test_run_stop_during_wait(monkeypatch, session):
    monkeypatch.setattr(config, "RECORDING_RESUME_GRACE_S", 30)
    monkeypatch.setattr(recorder, "_RESUME_CHECK_INTERVAL_S", 10)
    _fake_record(monkeypatch, [])
    monkeypatch.setattr(recorder, "is_live", lambda url: False)
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    t0 = time.monotonic()
    Recorder(session, stop).run()
    assert time.monotonic() - t0 < 2


def test_run_unexpected_error_sets_stop(monkeypatch, session):
    def boom(self, segment):
        raise RuntimeError("x")

    monkeypatch.setattr(Recorder, "_record_segment", boom)
    stop = threading.Event()
    Recorder(session, stop).run()
    assert stop.is_set()
