"""Testes do engine (JobManager, record_session, process_session) sem rede, ffmpeg ou whisper."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

import config
import engine
from clipper import ClipperError, ClipSpec
from engine import JobManager, JobState
from hype_detector import Spike
from session import Session

URL = "https://www.twitch.tv/canal"
URL2 = "https://www.twitch.tv/outro"
TERMINAL = (JobState.DONE, JobState.STOPPED, JobState.ERROR)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(config, "POLL_INTERVAL_S", 0.2)
    monkeypatch.setattr(config, "MAX_PARALLEL_LIVES", 4)
    monkeypatch.setattr(config, "POSTPROCESS_WORKERS", 1)
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    before = list(root.handlers)
    yield
    root.setLevel(old_level)
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()


# --- dublês --------------------------------------------------------------
class _FakeSource:
    pass


class _FakeChat:
    def __init__(self, session, source, stop_event):
        self.stop_event = stop_event
        self.message_count = 7
        self.reconnect_count = 0

    def run(self):
        engine.logger.info("chat fake rodando")
        self.stop_event.wait()


class _WaitingRecorder:
    def __init__(self, session, stop_event):
        self.session = session
        self.stop_event = stop_event

    def run(self):
        engine.logger.info("recorder fake de %s", self.session.channel)
        seg = self.session.new_segment()
        seg.path.write_bytes(b"x" * 100)
        self.stop_event.wait()


class _QuickRecorder:
    def __init__(self, session, stop_event):
        self.stop_event = stop_event

    def run(self):
        self.stop_event.set()


def _patch_record(monkeypatch, recorder=_WaitingRecorder):
    monkeypatch.setattr(engine, "Recorder", recorder)
    monkeypatch.setattr(engine, "ChatListener", _FakeChat)
    monkeypatch.setattr(engine, "make_chat_source", lambda url: _FakeSource())


def _wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _Collector:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()

    def __call__(self, status):
        with self.lock:
            self.items.append(status)

    def states(self, job_id=None):
        with self.lock:
            seq = []
            for s in self.items:
                if job_id and s.job_id != job_id:
                    continue
                if not seq or seq[-1] != s.state:
                    seq.append(s.state)
            return seq


def _wait_state(manager, job_id, *states, timeout=10.0):
    ok = _wait_until(lambda: manager.status(job_id).state in states, timeout)
    assert ok, f"estado atual: {manager.status(job_id)}"


@pytest.fixture
def managers():
    created: list[JobManager] = []

    def make(on_update=None):
        m = JobManager(on_update=on_update)
        created.append(m)
        return m

    yield make
    for m in created:
        m.shutdown(wait=True)


# --- validação -----------------------------------------------------------
def test_add_invalid_url(managers):
    m = managers()
    with pytest.raises(ValueError, match="Link não reconhecido"):
        m.add("https://example.com/canal")
    with pytest.raises(ValueError):
        m.add("")


def test_add_duplicate_channel_and_limit(managers, monkeypatch):
    monkeypatch.setattr(engine, "is_live", lambda url: False)
    monkeypatch.setattr(config, "MAX_PARALLEL_LIVES", 2)
    m = managers()
    m.add(URL)
    with pytest.raises(ValueError, match="já está sendo monitorada"):
        m.add("https://twitch.tv/CANAL")
    m.add(URL2)
    with pytest.raises(RuntimeError, match="Limite"):
        m.add("https://kick.com/terceiro")


def test_add_notifies_waiting_immediately(managers, monkeypatch):
    monkeypatch.setattr(engine, "is_live", lambda url: False)
    col = _Collector()
    m = managers(col)
    job_id = m.add(URL)
    assert col.items and col.items[0].state == JobState.WAITING and col.items[0].job_id == job_id
    assert "·" not in col.items[0].message


def test_terminated_job_frees_slot_and_remove(managers, monkeypatch):
    monkeypatch.setattr(engine, "is_live", lambda url: False)
    monkeypatch.setattr(config, "MAX_PARALLEL_LIVES", 1)
    m = managers()
    a = m.add(URL)
    with pytest.raises(ValueError):
        m.remove(a)  # ainda ativo
    m.stop(a)
    m.stop(a)  # idempotente
    _wait_state(m, a, JobState.STOPPED)
    m.add(URL)  # canal igual, agora permitido
    m.remove(a)
    assert a not in [s.job_id for s in m.all()]


# --- ciclo completo ------------------------------------------------------
def _patch_post(monkeypatch, finals_per_call=1, delay=0.0, order=None, fail_for=()):
    def fake_process(session, *, model_cache=None, on_progress=None, stop_event=None):
        if order is not None:
            order.append(("start", session.channel, threading.current_thread().name))
        if session.channel in fail_for:
            raise RuntimeError("explodiu")
        on_progress({"spikes": 3, "clips_done": 0, "clips_total": finals_per_call})
        time.sleep(delay)
        out = []
        for i in range(finals_per_call):
            on_progress({"spikes": 3, "clips_done": i, "clips_total": finals_per_call})
            p = session.final_dir / f"c{i}.mp4"
            p.write_bytes(b"f")
            out.append(p)
        on_progress({"spikes": 3, "clips_done": finals_per_call, "clips_total": finals_per_call})
        session.status["postprocess"] = f"ok: {finals_per_call}/{finals_per_call} clipes"
        if order is not None:
            order.append(("end", session.channel, None))
        return out

    monkeypatch.setattr(engine, "process_session", fake_process)


def test_full_cycle_sequence(managers, monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    _patch_post(monkeypatch, finals_per_call=2)
    col = _Collector()
    m = managers(col)
    job_id = m.add(URL, once=True)
    _wait_state(m, job_id, *TERMINAL)
    assert col.states(job_id) == [
        JobState.WAITING, JobState.RECORDING, JobState.QUEUED, JobState.PROCESSING, JobState.DONE,
    ]
    final = m.status(job_id)
    assert final.state == JobState.DONE and final.message == "Pronto: 2 clipes"
    assert len(final.final_clips) == 2 and final.session_dir is not None
    assert final.spikes == 3 and final.clips_total == 2
    messages = [s.message for s in col.items]
    assert any(msg.startswith("Gerando clipe") for msg in messages)
    assert all("·" not in msg for msg in messages)


def test_no_spikes_message(managers, monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)

    def fake_process(session, **kw):
        session.status["postprocess"] = "ok: 0/0 clipes"
        return []

    monkeypatch.setattr(engine, "process_session", fake_process)
    m = managers()
    job_id = m.add(URL)
    _wait_state(m, job_id, *TERMINAL)
    st = m.status(job_id)
    assert st.state == JobState.DONE and st.message == "Nenhum momento de destaque encontrado"


def test_waiting_then_live_starts(managers, monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    calls = []
    monkeypatch.setattr(engine, "is_live", lambda url: (calls.append(1), len(calls) >= 3)[1])
    _patch_post(monkeypatch)
    m = managers()
    job_id = m.add(URL)
    _wait_state(m, job_id, *TERMINAL)
    assert len(calls) >= 3 and m.status(job_id).state == JobState.DONE


def test_stop_while_waiting_is_stopped(managers, monkeypatch):
    monkeypatch.setattr(engine, "is_live", lambda url: False)
    m = managers()
    job_id = m.add(URL)
    time.sleep(0.1)
    m.stop(job_id)
    _wait_state(m, job_id, JobState.STOPPED)


def test_stop_while_recording_goes_to_queue(managers, monkeypatch):
    _patch_record(monkeypatch, _WaitingRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    processed = []
    _patch_post(monkeypatch, order=processed)
    col = _Collector()
    m = managers(col)
    job_id = m.add(URL)
    _wait_state(m, job_id, JobState.RECORDING)
    assert _wait_until(lambda: m.status(job_id).chat_messages == 7)
    assert _wait_until(lambda: m.status(job_id).recorded_bytes == 100)
    m.stop(job_id)
    _wait_state(m, job_id, *TERMINAL)
    assert JobState.QUEUED in col.states(job_id)
    assert m.status(job_id).state == JobState.DONE
    assert processed and processed[0][0] == "start"


def test_two_jobs_parallel_queue_one_at_a_time(managers, monkeypatch):
    _patch_record(monkeypatch, _WaitingRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    order: list = []
    _patch_post(monkeypatch, delay=0.4, order=order)
    m = managers()
    a, b = m.add(URL), m.add(URL2)
    _wait_state(m, a, JobState.RECORDING)
    _wait_state(m, b, JobState.RECORDING)  # gravando em paralelo
    m.stop(a)
    m.stop(b)
    _wait_state(m, a, *TERMINAL)
    _wait_state(m, b, *TERMINAL)
    kinds = [o[0] for o in order]
    assert kinds == ["start", "end", "start", "end"]  # um de cada vez
    assert {o[1] for o in order} == {"canal", "outro"}
    assert all(o[2] == f"job-{jid}-post" for o, jid in zip(order[::2], [a, b])) or all(
        o[2].startswith("job-") and o[2].endswith("-post") for o in order[::2]
    )
    assert threading.current_thread().name != "job-x-post"


def test_post_thread_renamed_back(managers, monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    _patch_post(monkeypatch)
    m = managers()
    job_id = m.add(URL)
    _wait_state(m, job_id, *TERMINAL)
    assert any(t.name == "postprocess" for t in threading.enumerate())


def test_error_in_job_does_not_kill_others(managers, monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    lives = {URL: True, URL2: True}

    def is_live(url):
        if "canal" in url and url.endswith("canal"):
            raise RuntimeError("falha de rede inesperada")
        return True

    monkeypatch.setattr(engine, "is_live", is_live)
    _patch_post(monkeypatch)
    m = managers()
    bad, good = m.add(URL), m.add(URL2)
    _wait_state(m, bad, *TERMINAL)
    _wait_state(m, good, *TERMINAL)
    assert m.status(bad).state == JobState.ERROR and m.status(bad).error
    assert m.status(good).state == JobState.DONE
    assert lives


def test_processing_error_marks_error_and_queue_continues(managers, monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    _patch_post(monkeypatch, fail_for=("canal",))
    m = managers()
    bad = m.add(URL)
    good = m.add(URL2)
    _wait_state(m, bad, *TERMINAL)
    _wait_state(m, good, *TERMINAL)
    assert m.status(bad).state == JobState.ERROR and "explodiu" in m.status(bad).message
    assert m.status(good).state == JobState.DONE


def test_recorder_error_is_error_state(managers, monkeypatch):
    def is_live(url):
        raise engine.RecorderError("sem plugin")

    monkeypatch.setattr(engine, "is_live", is_live)
    m = managers()
    job_id = m.add(URL)
    _wait_state(m, job_id, JobState.ERROR)
    assert "sem plugin" in m.status(job_id).message


def test_on_update_exception_does_not_break(managers, monkeypatch):
    monkeypatch.setattr(engine, "is_live", lambda url: False)

    def boom(status):
        raise RuntimeError("ui quebrou")

    m = managers(boom)
    job_id = m.add(URL)
    m.stop(job_id)
    _wait_state(m, job_id, JobState.STOPPED)


def test_shutdown_discards_queue_and_is_fast(managers, monkeypatch):
    _patch_record(monkeypatch, _WaitingRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    _patch_post(monkeypatch, delay=0.5)
    m = managers()
    a, b = m.add(URL), m.add(URL2)
    _wait_state(m, a, JobState.RECORDING)
    _wait_state(m, b, JobState.RECORDING)
    began = time.monotonic()
    m.shutdown(wait=True)
    assert time.monotonic() - began < 30
    for jid in (a, b):
        assert m.status(jid).state in TERMINAL


# --- log por live ---------------------------------------------------------
def test_run_log_per_live_has_only_own_lines(managers, monkeypatch):
    _patch_record(monkeypatch, _WaitingRecorder)
    monkeypatch.setattr(engine, "is_live", lambda url: True)
    _patch_post(monkeypatch)
    m = managers()
    a, b = m.add(URL), m.add(URL2)
    _wait_state(m, a, JobState.RECORDING)
    _wait_state(m, b, JobState.RECORDING)
    assert _wait_until(lambda: m.status(a).recorded_bytes == 100 and m.status(b).recorded_bytes == 100)
    sa, sb = m.status(a).session_dir, m.status(b).session_dir
    m.stop(a)
    m.stop(b)
    _wait_state(m, a, *TERMINAL)
    _wait_state(m, b, *TERMINAL)
    log_a = (sa / "run.log").read_text(encoding="utf-8")
    log_b = (sb / "run.log").read_text(encoding="utf-8")
    assert "recorder fake de canal" in log_a and "outro" not in log_a
    assert "recorder fake de outro" in log_b and "recorder fake de canal" not in log_b
    assert f"job-{a}" in log_a and f"job-{b}" not in log_a
    assert f"job-{b}" in log_b and f"job-{a}" not in log_b
    assert "chat fake rodando" in log_a and "chat fake rodando" in log_b
    # os handlers de arquivo foram removidos ao fim do job
    leftover = [h for h in logging.getLogger().handlers if hasattr(h, engine._SESSION_LOG_TAG)]
    assert leftover == []


# --- record_session ------------------------------------------------------
def test_record_session_recorder_finishes_alone(monkeypatch):
    _patch_record(monkeypatch, _QuickRecorder)
    session = engine.record_session(URL, threading.Event())
    assert session.status["record"] == "ok" and session.manifest_path.exists()


def test_record_session_max_duration_and_progress(monkeypatch):
    _patch_record(monkeypatch, _WaitingRecorder)
    infos: list[dict] = []
    began = time.monotonic()
    session = engine.record_session(URL, threading.Event(), max_duration=1.0, on_progress=infos.append)
    assert 0.9 <= time.monotonic() - began < 6.0
    assert session.status["record"] == "ok"
    assert infos and "session_dir" in infos[0]
    assert infos[-1]["recorded_bytes"] == 100 and infos[-1]["chat_messages"] == 7


def test_record_session_stop_event_is_interrupted(monkeypatch):
    _patch_record(monkeypatch, _WaitingRecorder)
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    assert engine.record_session(URL, stop).status["record"] == "interrompido"


def test_record_session_chat_source_failure_records_video_only(monkeypatch):
    monkeypatch.setattr(engine, "Recorder", _QuickRecorder)

    def bad(url):
        raise RuntimeError("sem chat")

    monkeypatch.setattr(engine, "make_chat_source", bad)
    assert engine.record_session(URL, threading.Event()).status["record"] == "ok"


# --- process_session (antigo main.postprocess) -----------------------------
def _session_with_segment(tmp_path) -> Session:
    session = Session.create(URL, tmp_path)
    seg = session.new_segment()
    seg.path.write_bytes(b"x" * 10)
    return session


def _patch_pipeline(monkeypatch, specs, fail_render_on=(), no_audio=False):
    monkeypatch.setattr(engine, "find_spikes", lambda s: [Spike(0, 50.0, 20, 4.0)])
    monkeypatch.setattr(engine, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(engine, "plan_clips", lambda *a, **k: specs)
    seen: dict = {"wavs": [], "srts": {}}

    def extract(src, spec, wav):
        if no_audio:
            raise ClipperError("sem trilha de áudio")
        wav.write_bytes(b"w")
        seen["wavs"].append(wav)
        return wav

    monkeypatch.setattr(engine, "extract_audio", extract)
    monkeypatch.setattr(engine, "transcribe_to_srt", lambda m, wav, srt: (srt.write_text("s"), srt)[1])

    def render(src, spec, out, srt=None):
        if any(k in out.name for k in fail_render_on):
            raise RuntimeError("render falhou")
        seen["srts"][out.name] = srt
        out.write_bytes(b"f")
        return out

    monkeypatch.setattr(engine, "render_clip", render)
    return seen


def test_process_second_clip_fails(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    specs = [ClipSpec(0, 10.0, 30.0, 20), ClipSpec(0, 100.0, 30.0, 15)]
    _patch_pipeline(monkeypatch, specs, fail_render_on=("clip_02",))
    monkeypatch.setattr(engine, "load_model", lambda: object())
    progress: list[dict] = []
    finals = engine.process_session(session, on_progress=progress.append)
    assert [f.name for f in finals] == ["clip_01_seg0_00m10s.mp4"]
    assert session.status["postprocess"] == "parcial: 1/2 clipes"
    assert session.segments[0].path.exists()
    assert any(p.get("clips_total") == 2 for p in progress)
    assert progress[-1] == {"spikes": 1, "clips_done": 1, "clips_total": 2}


def test_process_no_spikes_does_not_load_model(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [])

    def boom():
        raise AssertionError("modelo não deveria ser carregado")

    monkeypatch.setattr(engine, "load_model", boom)
    assert engine.process_session(session) == []
    assert session.status["postprocess"].startswith("ok")


def test_process_model_failure(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])

    def fail():
        raise RuntimeError("sem modelo")

    monkeypatch.setattr(engine, "load_model", fail)
    assert engine.process_session(session) == []
    assert session.status["postprocess"].startswith("erro")


def test_process_deletes_raw_when_all_ok(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])
    monkeypatch.setattr(engine, "load_model", lambda: object())
    monkeypatch.setattr(config, "DELETE_RAW_AFTER_PROCESS", True)
    assert len(engine.process_session(session)) == 1
    assert not session.segments[0].path.exists()


def test_process_wav_deleted_and_srt_used(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    seen = _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])
    monkeypatch.setattr(engine, "load_model", lambda: object())
    assert len(engine.process_session(session)) == 1
    assert len(seen["wavs"]) == 1 and not seen["wavs"][0].exists()
    assert seen["srts"]["clip_01_seg0_00m10s.mp4"] is not None


def test_process_wav_deleted_even_if_render_fails(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    seen = _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)], fail_render_on=("clip_01",))
    monkeypatch.setattr(engine, "load_model", lambda: object())
    assert engine.process_session(session) == []
    assert not seen["wavs"][0].exists()
    assert session.status["postprocess"] == "erro: 0/1 clipes"


def test_process_segment_without_audio_renders_without_subs(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    seen = _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)], no_audio=True)
    monkeypatch.setattr(engine, "load_model", lambda: object())
    finals = engine.process_session(session)
    assert len(finals) == 1 and finals[0].exists()
    assert seen["srts"]["clip_01_seg0_00m10s.mp4"] is None
    assert session.status["postprocess"] == "ok: 1/1 clipes"


def test_process_model_cache_reused(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])
    loads: list[int] = []
    monkeypatch.setattr(engine, "load_model", lambda: loads.append(1) or object())
    cache: dict = {}
    engine.process_session(session, model_cache=cache)
    engine.process_session(session, model_cache=cache)
    assert len(loads) == 1


def test_process_stop_event_between_clips(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    specs = [ClipSpec(0, 10.0, 30.0, 20), ClipSpec(0, 100.0, 30.0, 15)]
    _patch_pipeline(monkeypatch, specs)
    monkeypatch.setattr(engine, "load_model", lambda: object())
    stop = threading.Event()
    stop.set()
    assert engine.process_session(session, stop_event=stop) == []


def test_process_log_goes_to_session_run_log(tmp_path, monkeypatch):
    session = _session_with_segment(tmp_path)
    _patch_pipeline(monkeypatch, [ClipSpec(0, 10.0, 30.0, 20)])
    monkeypatch.setattr(engine, "load_model", lambda: object())
    engine.process_session(session)
    assert "clipe 1/1 pronto" in session.log_path.read_text(encoding="utf-8")
