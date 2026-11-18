"""Encerramento sem órfãos: registro de processos, Job Object e shutdown do JobManager;
e o app.log da GUI empacotada."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

import pytest

import config
import engine
import recorder
import runtime
from engine import JobManager
from session import Session

URLS = [f"https://www.twitch.tv/canal{n}" for n in range(4)]


# --- registro de processos do recorder -------------------------------------
class _StubbornPopen:
    """Popen falso que ignora terminate() e só morre com kill()."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.terminated = 0
        self.killed = 0
        self._dead = False

    def poll(self):
        return 0 if self._dead else None

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1
        self._dead = True

    def wait(self, timeout=None):
        if self._dead:
            return 0
        raise subprocess.TimeoutExpired("fake", timeout or 0)


class _PoliteMixin(_StubbornPopen):
    def terminate(self) -> None:
        super().terminate()
        self._dead = True


@pytest.fixture
def clean_registry():
    with recorder._LIVE_PROCS_LOCK:
        recorder._LIVE_PROCS.clear()
    yield
    with recorder._LIVE_PROCS_LOCK:
        recorder._LIVE_PROCS.clear()


def test_terminate_all_kills_process_that_ignores_terminate(clean_registry, monkeypatch):
    monkeypatch.setattr(runtime, "assign_to_kill_on_close_job", lambda proc, job=None: False)
    stubborn, polite = _StubbornPopen(1), _PoliteMixin(2)
    recorder._register_process(stubborn)
    recorder._register_process(polite)
    assert recorder.terminate_all_processes(timeout=0.2) == 2
    assert stubborn.terminated == 1 and stubborn.killed == 1
    assert polite.terminated == 1 and polite.killed == 0
    assert recorder._LIVE_PROCS == set()
    assert recorder.terminate_all_processes(timeout=0.1) == 0   # idempotente


def test_unregister_removes_process(clean_registry, monkeypatch):
    monkeypatch.setattr(runtime, "assign_to_kill_on_close_job", lambda proc, job=None: False)
    proc = _StubbornPopen()
    recorder._register_process(proc)
    recorder._unregister_process(proc)
    assert recorder.terminate_all_processes(timeout=0.1) == 0
    assert proc.terminated == 0


# --- Job Object (Windows) ---------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Job Object é do Windows")
def test_job_object_kills_child_on_close():
    job = runtime.create_kill_on_close_job()
    assert job is not None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=runtime.subprocess_flags())
    try:
        assert runtime.assign_to_kill_on_close_job(child, job)
        assert child.poll() is None
        runtime.close_job(job)
        child.wait(timeout=10)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_job_helpers_never_raise_without_job():
    assert runtime.assign_to_kill_on_close_job(_StubbornPopen(), None) in (True, False)
    runtime.close_job(None)


# --- JobManager.shutdown ----------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(config, "POLL_INTERVAL_S", 0.2)
    monkeypatch.setattr(config, "MAX_PARALLEL_LIVES", 4)
    monkeypatch.setattr(config, "POSTPROCESS_WORKERS", 1)
    monkeypatch.setattr(engine, "is_live", lambda url: True)


class _Chat:
    message_count = 0
    reconnect_count = 0

    def __init__(self, session, source, stop_event):
        self.stop_event = stop_event

    def run(self):
        self.stop_event.wait()


class _Recorder:
    """Grava até o stop; leva ~0.3 s para encerrar (como o terminate do streamlink)."""

    def __init__(self, session, stop_event):
        self.session, self.stop_event = session, stop_event

    def run(self):
        seg = self.session.new_segment()
        seg.path.write_bytes(b"x" * 10)
        seg.wall_start = time.time()
        self.stop_event.wait()
        time.sleep(0.3)
        seg.wall_end = time.time()
        self.session.save()


def _patch_engine(monkeypatch):
    monkeypatch.setattr(engine, "Recorder", _Recorder)
    monkeypatch.setattr(engine, "ChatListener", _Chat)
    monkeypatch.setattr(engine, "make_chat_source", lambda url: object())


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_shutdown_four_parallel_jobs_within_ceiling(monkeypatch):
    _patch_engine(monkeypatch)
    kills = []
    monkeypatch.setattr(engine, "terminate_all_processes", lambda *a, **k: kills.append(1) or 0)
    m = JobManager()
    ids = [m.add(u) for u in URLS]
    assert _wait(lambda: all(m.status(i).state == engine.JobState.RECORDING for i in ids))
    assert _wait(lambda: all(j.recording_session is not None for j in m._jobs.values()))
    began = time.monotonic()
    m.shutdown(wait=True)
    elapsed = time.monotonic() - began
    assert elapsed < engine._SHUTDOWN_WAIT_S
    assert elapsed < 10   # em paralelo: ~1 job de latência, não 4 em série
    assert kills, "o shutdown deve acionar terminate_all_processes"
    sessions = [j.recording_session for j in m._jobs.values()]
    assert len(sessions) == 4
    for sess in sessions:
        assert sess.status["record"] == "interrompido"
        assert all(seg.wall_end is not None for seg in sess.segments)
        reloaded = Session.load(sess.root)
        assert reloaded.status["record"] == "interrompido"
        assert all(seg.wall_end is not None for seg in reloaded.segments)


def test_shutdown_ceiling_closes_stuck_sessions(monkeypatch):
    """Gravação travada (ignora a parada): estourado o teto, a sessão é fechada mesmo assim."""
    release = threading.Event()
    sessions: list[Session] = []

    def stuck_record(url, stop_event, *, on_session=None, **kwargs):
        session = Session.create(url, config.SESSIONS_DIR)
        session.new_segment()
        sessions.append(session)
        if on_session is not None:
            on_session(session)
        release.wait(5.0)           # ignora stop_event
        return session

    monkeypatch.setattr(engine, "record_session", stuck_record)
    monkeypatch.setattr(engine, "_SHUTDOWN_WAIT_S", 0.5)
    monkeypatch.setattr(engine, "_SHUTDOWN_GRACE_S", 0.2)
    calls = []
    monkeypatch.setattr(engine, "terminate_all_processes", lambda *a, **k: calls.append(1) or 0)
    m = JobManager()
    ids = [m.add(u) for u in URLS]
    assert _wait(lambda: len(sessions) == 4)
    try:
        m.shutdown(wait=True)
    finally:
        release.set()
    assert calls
    for sess in sessions:
        assert sess.status["record"] == "interrompido"
        assert all(seg.wall_end is not None for seg in sess.segments)
        assert Session.load(sess.root).status["record"] == "interrompido"
    assert ids


# --- app.log da GUI empacotada ------------------------------------------------
def test_app_log_receives_lines_after_session_handler_attach_detach(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import gui

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime, "data_dir", lambda: tmp_path)
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        gui.setup_gui_logging()
        gui.setup_gui_logging()   # idempotente: um único handler de app.log
        assert len(gui._app_log_handlers()) == 1
        logging.getLogger("engine").info("linha-antes")
        handler = engine._attach_session_log(tmp_path / "run.log", "job-abc")
        done = threading.Event()

        def worker():
            logging.getLogger("engine").info("linha-do-job")
            done.set()

        threading.Thread(target=worker, name="job-abc-recorder").start()
        assert done.wait(5)
        engine._detach_session_log(handler)
        logging.getLogger("engine").info("linha-depois")
        # handler de app.log removido por engano: ensure_app_log o recoloca
        for h in gui._app_log_handlers():
            root.removeHandler(h)
            h.close()
        assert gui.ensure_app_log()
        logging.getLogger("engine").info("linha-recolocada")
        text = (tmp_path / "app.log").read_text(encoding="utf-8")
        for expected in ("linha-antes", "linha-do-job", "linha-depois", "linha-recolocada"):
            assert expected in text
        run_text = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "linha-do-job" in run_text and "linha-antes" not in run_text
    finally:
        for h in list(root.handlers):
            if h not in saved_handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(saved_level)
