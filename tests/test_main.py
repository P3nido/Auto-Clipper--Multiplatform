"""Testes da CLI (main.py). A lógica de gravação/clipes é testada em test_engine.py."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import config
import engine
import hardware
import main
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


class _FakeManager:
    instances: list["_FakeManager"] = []

    def __init__(self, on_update=None):
        self.on_update = on_update
        self.added: list[tuple[str, bool, float | None]] = []
        self.stopped: list[str] = []
        self.states: dict[str, engine.JobState] = {}
        self.shutdowns = 0
        _FakeManager.instances.append(self)

    def add(self, url, *, once=True, max_duration=None):
        job_id = f"j{len(self.added)}"
        self.added.append((url, once, max_duration))
        self.states[job_id] = engine.JobState.DONE
        return job_id

    def status(self, job_id):
        return engine.JobStatus(job_id, "u", "twitch", "c", self.states[job_id], "m")

    def stop(self, job_id):
        self.stopped.append(job_id)

    def shutdown(self, wait=True):
        self.shutdowns += 1


@pytest.fixture
def fake_manager(monkeypatch):
    _FakeManager.instances = []
    monkeypatch.setattr(main, "JobManager", _FakeManager)
    monkeypatch.setattr(main, "check_prerequisites", lambda url=None: None)
    return _FakeManager


def test_multiple_urls_create_multiple_jobs(fake_manager):
    code = main.main(["--url", URL, "--url", "https://kick.com/outro", "--once", "--max-duration", "5"])
    assert code == 0
    mgr = fake_manager.instances[0]
    assert mgr.added == [(URL, True, 5.0), ("https://kick.com/outro", True, 5.0)]
    assert mgr.shutdowns >= 1


def test_default_url_from_config(fake_manager, monkeypatch):
    monkeypatch.setattr(config, "STREAM_URL", URL)
    assert main.main([]) == 0
    assert fake_manager.instances[0].added == [(URL, False, None)]


def test_error_job_returns_1(fake_manager, monkeypatch):
    orig = _FakeManager.add

    def add(self, url, **kw):
        job_id = orig(self, url, **kw)
        self.states[job_id] = engine.JobState.ERROR
        return job_id

    monkeypatch.setattr(_FakeManager, "add", add)
    assert main.main(["--url", URL]) == 1


def test_add_rejection_returns_1(fake_manager, monkeypatch):
    def add(self, url, **kw):
        raise ValueError("duplicado")

    monkeypatch.setattr(_FakeManager, "add", add)
    assert main.main(["--url", URL]) == 1


def test_diagnostico_prints_report(monkeypatch, capsys):
    monkeypatch.setattr(main, "_resolve_ffmpeg_tools", lambda: None)
    monkeypatch.setattr(hardware, "diagnose", lambda: "RELATORIO FALSO")
    assert main.main(["--diagnostico"]) == 0
    assert "RELATORIO FALSO" in capsys.readouterr().out


def test_process_calls_process_session(tmp_path, monkeypatch):
    session = Session.create(URL, tmp_path)
    calls = []

    def fake(s, **kw):
        calls.append(s.root)
        s.status["postprocess"] = "ok: 0/0 clipes"
        return []

    monkeypatch.setattr(main, "check_prerequisites", lambda url=None: None)
    monkeypatch.setattr(main, "process_session", fake)
    assert main.main(["--process", str(session.root)]) == 0
    assert calls == [session.root]


def test_process_error_status_returns_1(tmp_path, monkeypatch):
    session = Session.create(URL, tmp_path)

    def fake(s, **kw):
        s.status["postprocess"] = "erro: sem modelo"
        return []

    monkeypatch.setattr(main, "check_prerequisites", lambda url=None: None)
    monkeypatch.setattr(main, "process_session", fake)
    assert main.main(["--process", str(session.root)]) == 1


def test_process_missing_session_returns_1(tmp_path):
    assert main.main(["--process", str(tmp_path / "nao_existe")]) == 1


def test_progress_logger_dedups(caplog):
    plog = main._ProgressLogger()
    st = engine.JobStatus("a", "u", "twitch", "canal", engine.JobState.WAITING, "Aguardando a live começar")
    with caplog.at_level(logging.INFO, logger="auto_clipper"):
        plog(st)
        plog(st)
        plog(st)
    assert len([r for r in caplog.records if r.name == "auto_clipper"]) == 1


def test_check_prerequisites_invalid_url():
    with pytest.raises(main.PrerequisiteError):
        main.check_prerequisites("https://example.com/canal")


def test_check_prerequisites_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(config, "FFMPEG_BIN", "ffmpeg-inexistente-xyz")
    monkeypatch.setattr(main.sys, "platform", "linux")
    with pytest.raises(main.PrerequisiteError, match="winget"):
        main.check_prerequisites(URL)


def test_check_prerequisites_faster_missing(monkeypatch):
    monkeypatch.setattr(main, "_resolve_ffmpeg_tools", lambda: None)
    real = main.importlib.util.find_spec
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    monkeypatch.setattr(
        main.importlib.util, "find_spec",
        lambda n, *a: None if n == "faster_whisper" else real(n, *a),
    )
    with pytest.raises(main.PrerequisiteError, match="faster-whisper"):
        main.check_prerequisites(URL)


def test_check_prerequisites_unknown_engine(monkeypatch):
    monkeypatch.setattr(main, "_resolve_ffmpeg_tools", lambda: None)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "xyz")
    with pytest.raises(main.PrerequisiteError, match="desconhecido"):
        main.check_prerequisites(URL)


def test_check_prerequisites_runs_hardware_detect(monkeypatch):
    monkeypatch.setattr(main, "_resolve_ffmpeg_tools", lambda: None)
    monkeypatch.setattr(config, "WHISPER_ENGINE", "faster")
    called = []
    prof = hardware.HardwareProfile("cpu", None, "cpu", "int8", "small", notes=("n",))
    monkeypatch.setattr(hardware, "detect", lambda force=False: called.append(1) or prof)
    main.check_prerequisites(URL)
    assert called


def test_main_returns_1_on_prerequisite_error(monkeypatch):
    def fail(url=None):
        raise main.PrerequisiteError("falta algo")

    monkeypatch.setattr(main, "check_prerequisites", fail)
    assert main.main(["--url", URL]) == 1


def test_setup_logging_idempotent(tmp_path):
    main.setup_logging(tmp_path / "a.log")
    main.setup_logging(tmp_path / "b.log")
    handlers = [
        h for h in logging.getLogger().handlers
        if getattr(h, main._HANDLER_TAG, None) == "file"
    ]
    assert len(handlers) == 1
    assert Path(handlers[0].baseFilename).name == "b.log"
