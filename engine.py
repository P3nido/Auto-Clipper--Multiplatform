"""Motor do Auto-Clipper: várias lives em paralelo e uma fila de clipes (ADR-008).

A CLI (main.py) e a interface (gui.py) são apenas CLIENTES deste módulo.
Contrato para a interface: JobManager + JobStatus + JobState. O callback
`on_update` é chamado de threads do motor — a interface deve repassar para a
thread de UI (ex.: Qt Signal). Nunca bloqueie dentro do callback.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import config
from chat_listener import ChatListener, channel_from_url, detect_platform, make_chat_source
from clipper import ClipperError, extract_audio, plan_clips, probe_duration, render_clip
from hype_detector import find_spikes
from recorder import Recorder, RecorderError, is_live
from session import Session, now
from transcriber import load_model, transcribe_to_srt

__all__ = [
    "JobState",
    "JobStatus",
    "JobManager",
    "record_session",
    "process_session",
]

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
_SESSION_LOG_TAG = "_auto_clipper_session_log"
_POLL_SLICE_S = 0.5
_SHUTDOWN_WAIT_S = 55.0   # teto total de shutdown(wait=True); a UI espera até 90 s
_PROGRESS_INTERVAL_S = 2.0
_RECORDER_JOIN_S = 15.0
_CHAT_JOIN_S = 10.0
_JOB_PREFIX = "job-"
_POST_THREAD_NAME = "postprocess"
_JOB_TAG_RE = re.compile(r"^(job-[0-9a-f]+)(?:-|$)")

_model_lock = threading.Lock()


class JobState(str, Enum):
    WAITING = "aguardando"        # monitorando: o canal ainda não está ao vivo
    RECORDING = "gravando"        # gravando vídeo + chat
    QUEUED = "na_fila"            # gravação terminou; esperando a vez na fila de clipes
    PROCESSING = "processando"    # detectando picos / gerando clipes
    DONE = "concluido"            # terminou (once=True) — clipes prontos
    STOPPED = "parado"            # usuário parou antes de gravar algo
    ERROR = "erro"


_TERMINAL_STATES = (JobState.DONE, JobState.STOPPED, JobState.ERROR)


@dataclass(frozen=True)
class JobStatus:
    """Snapshot imutável do estado de uma live (seguro para passar entre threads)."""

    job_id: str
    url: str
    platform: str                 # "twitch" | "kick" | "youtube"
    channel: str
    state: JobState
    message: str                  # frase curta pt-BR para a UI, ex.: "Gravando há 12:34"
    recorded_s: float = 0.0
    recorded_bytes: int = 0
    chat_messages: int = 0
    spikes: int = 0
    clips_done: int = 0
    clips_total: int = 0
    session_dir: Path | None = None
    final_clips: tuple[Path, ...] = ()
    error: str | None = None
    updated_at: float = 0.0


# --------------------------------------------------------------------------
# Formatação de mensagens (pt-BR)
# --------------------------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _fmt_size(num_bytes: int) -> str:
    gb = num_bytes / 1024 ** 3
    if gb >= 1:
        return f"{gb:.1f} GB".replace(".", ",")
    return f"{num_bytes / 1024 ** 2:.0f} MB"


def _fmt_count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


_RECORDING_MESSAGE = "Gravando vídeo e chat"


# --------------------------------------------------------------------------
# Log por live: um FileHandler por sessão, filtrado pelo nome da thread
# --------------------------------------------------------------------------
class _LogFormatter(logging.Formatter):
    """O streamlink renomeia os níveis globais para minúsculo ao ser importado;
    normaliza para que o run.log fique consistente (INFO, WARNING, ERROR)."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = record.levelname.upper()
        return super().format(record)


class _ThreadPrefixFilter(logging.Filter):
    """Aceita só registros emitidos por threads `<tag>` ou `<tag>-...`."""

    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.threadName or ""
        return name == self.tag or name.startswith(self.tag + "-")


def _attach_session_log(log_path: Path, tag: str) -> logging.Handler:
    root = logging.getLogger()
    level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO)
    if root.getEffectiveLevel() > level:
        root.setLevel(level)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(_LogFormatter(_LOG_FORMAT))
    handler.addFilter(_ThreadPrefixFilter(tag))
    setattr(handler, _SESSION_LOG_TAG, tag)
    root.addHandler(handler)
    return handler


def _detach_session_log(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    try:
        handler.flush()
    finally:
        handler.close()


def _current_job_tag() -> str:
    """Tag de log da thread atual: `job-<id>` (de `job-<id>-post`) ou o nome inteiro."""
    name = threading.current_thread().name
    match = _JOB_TAG_RE.match(name)
    return match.group(1) if match else name


# --------------------------------------------------------------------------
# Gravação
# --------------------------------------------------------------------------
def _recording_stats(session: Session, listener: ChatListener | None) -> dict:
    total_bytes = 0
    recorded_s = 0.0
    current = now()
    for seg in list(session.segments):
        try:
            if seg.path.exists():
                total_bytes += seg.path.stat().st_size
        except OSError:
            pass
        if seg.wall_start is not None:
            recorded_s += max(0.0, (seg.wall_end if seg.wall_end is not None else current) - seg.wall_start)
    return {
        "recorded_s": recorded_s,
        "recorded_bytes": total_bytes,
        "chat_messages": int(getattr(listener, "message_count", 0)) if listener else 0,
    }


def record_session(url: str, stop_event, *, max_duration: float | None = None,
                   on_progress: Callable[[dict], None] | None = None, job_tag: str = "job"):
    """Grava UMA live (antigo main.run_session, sem o tratamento de Ctrl+C, que é da CLI).
    Threads nomeadas com o prefixo `job_tag`; o run.log da sessão só recebe linhas
    desse prefixo. on_progress recebe dicts {"recorded_s","recorded_bytes","chat_messages"}
    (mais "session_dir" na primeira chamada). Retorna a Session."""
    session = Session.create(url, Path(config.SESSIONS_DIR))
    log_handler = _attach_session_log(session.log_path, job_tag)
    try:
        return _record_into(session, url, stop_event, max_duration, on_progress, job_tag)
    finally:
        _detach_session_log(log_handler)


def _record_into(session: Session, url: str, stop_event, max_duration: float | None,
                 on_progress: Callable[[dict], None] | None, job_tag: str) -> Session:
    logger.info("Sessão iniciada em %s", session.root)
    # O Recorder/ChatListener usam um evento interno (o Recorder o seta ao terminar);
    # o stop_event do chamador é só o pedido de parada.
    inner_stop = threading.Event()

    recorder = Recorder(session, inner_stop)
    rec_thread = threading.Thread(target=recorder.run, name=f"{job_tag}-recorder", daemon=True)

    listener: ChatListener | None = None
    chat_thread: threading.Thread | None = None
    try:
        source = make_chat_source(url)
        listener = ChatListener(session, source, inner_stop)
        chat_thread = threading.Thread(target=listener.run, name=f"{job_tag}-chat", daemon=True)
    except Exception:  # noqa: BLE001 - sem chat ainda dá para gravar o vídeo
        logger.exception("Não foi possível criar a fonte de chat; gravando só o vídeo")

    def emit(first: bool = False) -> None:
        if on_progress is None:
            return
        info = _recording_stats(session, listener)
        if first:
            info["session_dir"] = session.root
        try:
            on_progress(info)
        except Exception:  # noqa: BLE001 - callback de UI nunca derruba a gravação
            logger.exception("on_progress falhou")

    interrupted = False
    started = time.monotonic()
    next_progress = started
    rec_thread.start()
    if chat_thread is not None:
        chat_thread.start()
    emit(first=True)
    while rec_thread.is_alive():
        rec_thread.join(_POLL_SLICE_S)
        if stop_event.is_set():
            interrupted = True
            logger.info("parada solicitada; encerrando a gravação")
            break
        if max_duration is not None and time.monotonic() - started >= max_duration:
            logger.info("max-duration de %.1f s atingido; encerrando", max_duration)
            break
        if time.monotonic() >= next_progress:
            emit()
            next_progress = time.monotonic() + _PROGRESS_INTERVAL_S

    inner_stop.set()
    rec_thread.join(_RECORDER_JOIN_S)
    if chat_thread is not None:
        chat_thread.join(_CHAT_JOIN_S)
    if rec_thread.is_alive():
        logger.warning("Recorder não encerrou em %.0f s", _RECORDER_JOIN_S)

    session.status["record"] = "interrompido" if interrupted else "ok"
    session.save()
    stats = _recording_stats(session, listener)
    logger.info(
        "Gravação encerrada: %d segmento(s), %d bytes, %d mensagens de chat, %d reconexões",
        len(session.segments), stats["recorded_bytes"], stats["chat_messages"],
        listener.reconnect_count if listener else 0,
    )
    emit()
    return session


# --------------------------------------------------------------------------
# Pós-processamento
# --------------------------------------------------------------------------
def _clip_name(index: int, segment_index: int, start_s: float) -> str:
    total = int(start_s)
    return f"clip_{index:02d}_seg{segment_index}_{total // 60:02d}m{total % 60:02d}s"


def _get_model(model_cache: dict | None):
    if model_cache is None:
        return load_model()
    with _model_lock:
        if "model" not in model_cache:
            model_cache["model"] = load_model()
        return model_cache["model"]


def process_session(session, *, model_cache: dict | None = None,
                    on_progress: Callable[[dict], None] | None = None,
                    stop_event: threading.Event | None = None) -> list[Path]:
    """Pós-processamento de UMA sessão (antigo main.postprocess). on_progress recebe
    {"spikes","clips_done","clips_total"}. model_cache reaproveita o modelo Whisper
    entre chamadas. Retorna os clipes finais.

    Extensão compatível do contrato: `stop_event` (opcional) interrompe entre clipes
    (usado pelo JobManager.shutdown); o resultado fica "parcial"."""
    log_handler = _attach_session_log(session.log_path, _current_job_tag())
    try:
        return _process_into(session, model_cache, on_progress, stop_event)
    finally:
        _detach_session_log(log_handler)


def _process_into(session: Session, model_cache: dict | None,
                  on_progress: Callable[[dict], None] | None,
                  stop_event: threading.Event | None) -> list[Path]:
    def emit(**info: int) -> None:
        if on_progress is None:
            return
        try:
            on_progress(info)
        except Exception:  # noqa: BLE001
            logger.exception("on_progress falhou")

    spikes = find_spikes(session)
    emit(spikes=len(spikes), clips_done=0, clips_total=0)

    durations: dict[int, float] = {}
    sources: dict[int, Path] = {}
    for seg in session.segments:
        try:
            if not seg.path.exists() or seg.path.stat().st_size <= 0:
                logger.warning("Segmento %d ausente ou vazio; ignorado", seg.index)
                continue
            durations[seg.index] = probe_duration(seg.path)
            sources[seg.index] = seg.path
        except Exception as exc:  # noqa: BLE001
            logger.warning("Segmento %d: não foi possível medir a duração (%s); ignorado", seg.index, exc)

    specs = plan_clips(
        spikes, durations,
        pre_s=config.CLIP_PRE_S, post_s=config.CLIP_POST_S, max_clips=config.MAX_CLIPS,
    )
    if not specs:
        logger.info("Nenhum clipe a gerar (%d pico(s))", len(spikes))
        session.status["postprocess"] = "ok: 0/0 clipes"
        session.save()
        emit(spikes=len(spikes), clips_done=0, clips_total=0)
        return []

    total = len(specs)
    emit(spikes=len(spikes), clips_done=0, clips_total=total)
    try:
        model = _get_model(model_cache)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao carregar o modelo Whisper")
        session.status["postprocess"] = f"erro: {exc}"
        session.save()
        return []

    finals: list[Path] = []
    stopped_early = False
    for i, spec in enumerate(specs, start=1):
        if stop_event is not None and stop_event.is_set():
            logger.info("Encerrando antes do clipe %d/%d (parada solicitada)", i, total)
            stopped_early = True
            break
        emit(spikes=len(spikes), clips_done=i - 1, clips_total=total)
        began = time.monotonic()
        name = _clip_name(i, spec.segment_index, spec.start_s)
        src = sources[spec.segment_index]
        wav = session.clips_dir / f"{name}.wav"
        try:
            srt: Path | None = None
            try:
                extract_audio(src, spec, wav)
            except ClipperError as exc:
                logger.warning("clipe %s sem áudio utilizável (%s); renderizando sem legenda", name, exc)
            else:
                srt = transcribe_to_srt(model, wav, session.subs_dir / f"{name}.srt")
            final = render_clip(src, spec, session.final_dir / f"{name}.mp4", srt)
        except Exception:  # noqa: BLE001 - um clipe ruim não derruba os outros
            logger.exception("clipe %d/%d falhou (%s)", i, total, name)
            continue
        finally:
            wav.unlink(missing_ok=True)
        finals.append(final)
        logger.info("clipe %d/%d pronto (%.1f s)", i, total, time.monotonic() - began)

    done = len(finals)
    outcome = "ok" if done == total else ("parcial" if done else "erro")
    session.status["postprocess"] = f"{outcome}: {done}/{total} clipes"
    if config.DELETE_RAW_AFTER_PROCESS and done == total and not stopped_early:
        for path in sources.values():
            try:
                path.unlink()
                logger.info("Gravação bruta apagada: %s", path)
            except OSError as exc:
                logger.warning("Não foi possível apagar %s: %s", path, exc)
    session.save()
    emit(spikes=len(spikes), clips_done=done, clips_total=total)
    return finals


# --------------------------------------------------------------------------
# JobManager
# --------------------------------------------------------------------------
class _Job:
    """Estado interno de um job (a UI só vê JobStatus)."""

    def __init__(self, status: JobStatus, once: bool, max_duration: float | None) -> None:
        self.status = status
        self.once = once
        self.max_duration = max_duration
        self.stop_event = threading.Event()
        self.done_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.session: Session | None = None
        self.outcome: dict = {}
        self.processing = False

    @property
    def tag(self) -> str:
        return f"{_JOB_PREFIX}{self.status.job_id}"


class JobManager:
    """Gerencia N lives. Thread-safe.

    - add(url): valida a URL (ValueError com mensagem amigável se não suportada
      ou se já houver um job ativo para o mesmo canal; RuntimeError se passar de
      config.MAX_PARALLEL_LIVES jobs ativos) e inicia uma thread
      `job-<id>` que: espera a live (WAITING, checando a cada POLL_INTERVAL_S) →
      grava (RECORDING; status atualizado ao menos a cada 2 s com tempo, bytes e
      mensagens) → enfileira (QUEUED) → quando a fila chega nele, PROCESSING com
      clips_done/clips_total → DONE (once=True) ou volta a WAITING (once=False).
    - stop(job_id): se WAITING → STOPPED; se RECORDING → para a gravação e o que
      foi gravado segue para a fila normalmente. Idempotente.
    - remove(job_id): só para jobs terminados (DONE/STOPPED/ERROR); tira da lista.
    - Uma única thread `postprocess` consome a fila (config.POSTPROCESS_WORKERS);
      o modelo Whisper é carregado uma vez e reaproveitado entre lives.
    - shutdown(): para tudo; se wait=True, espera as threads (o que já estava na
      fila de clipes é descartado com log, a gravação bruta fica no disco para
      `--process` depois).
    - on_update(JobStatus) é chamado a cada mudança relevante (de qualquer thread).

    Detalhes: o job espera o pós-processamento da própria gravação antes de voltar a
    monitorar (once=False). stop() em um job QUEUED/PROCESSING não cancela os clipes.
    stop()/remove() de um id desconhecido são ignorados; status() levanta KeyError.
    O callback roda sob um lock do motor (ordem garantida): não bloqueie nele.
    """

    def __init__(self, on_update: Callable[[JobStatus], None] | None = None) -> None:
        self._on_update = on_update
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._shutdown = threading.Event()
        self._model_cache: dict = {}
        workers = max(1, int(config.POSTPROCESS_WORKERS))
        self._workers = [
            threading.Thread(
                target=self._post_loop,
                name=_POST_THREAD_NAME if workers == 1 else f"{_POST_THREAD_NAME}-{n}",
                daemon=True,
            )
            for n in range(1, workers + 1)
        ]
        for worker in self._workers:
            worker.start()

    # --- API pública ------------------------------------------------------
    def add(self, url: str, *, once: bool = True, max_duration: float | None = None) -> str:
        """Retorna o job_id (string curta, ex. 'a1b2c3')."""
        url = (url or "").strip()
        try:
            platform = detect_platform(url)
            channel = channel_from_url(url)
        except ValueError as exc:
            raise ValueError(
                f"Link não reconhecido: use o link de um canal da Twitch, YouTube ou Kick ({exc})"
            ) from exc
        with self._lock:
            if self._shutdown.is_set():
                raise RuntimeError("O motor está sendo encerrado; não é possível adicionar lives")
            active = [j for j in self._jobs.values() if j.status.state not in _TERMINAL_STATES]
            for job in active:
                if job.status.platform == platform and job.status.channel.lower() == channel.lower():
                    raise ValueError(f"A live de {channel} ({platform}) já está sendo monitorada")
            if len(active) >= int(config.MAX_PARALLEL_LIVES):
                raise RuntimeError(
                    f"Limite de {config.MAX_PARALLEL_LIVES} lives ao mesmo tempo atingido; "
                    "pare uma live antes de adicionar outra"
                )
            job_id = uuid.uuid4().hex[:6]
            while job_id in self._jobs:
                job_id = uuid.uuid4().hex[:6]
            status = JobStatus(
                job_id=job_id, url=url, platform=platform, channel=channel,
                state=JobState.WAITING, message="Aguardando a live começar", updated_at=now(),
            )
            job = _Job(status, once, max_duration)
            job.thread = threading.Thread(
                target=self._job_main, args=(job,), name=job.tag, daemon=True
            )
            self._jobs[job_id] = job
            self._notify(status)
        logger.info("Live adicionada: %s (%s, job %s)", channel, platform, job_id)
        job.thread.start()
        return job_id

    def stop(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            logger.debug("stop(%s): job desconhecido", job_id)
            return
        if not job.stop_event.is_set():
            logger.info("Parada solicitada para o job %s (%s)", job_id, job.status.channel)
        job.stop_event.set()

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status.state not in _TERMINAL_STATES:
                raise ValueError("Só é possível remover lives já terminadas; pare a live primeiro")
            del self._jobs[job_id]

    def status(self, job_id: str) -> JobStatus:
        with self._lock:
            return self._jobs[job_id].status

    def all(self) -> list[JobStatus]:
        """Snapshots de todos os jobs, na ordem em que foram adicionados."""
        with self._lock:
            return [job.status for job in self._jobs.values()]

    def shutdown(self, wait: bool = True) -> None:
        self._shutdown.set()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.stop_event.set()
        if not wait:
            return
        deadline = time.monotonic() + _SHUTDOWN_WAIT_S
        for thread in [j.thread for j in jobs] + list(self._workers):
            if thread is None or thread is threading.current_thread():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logger.warning("Thread %s não encerrou a tempo no shutdown", thread.name)

    # --- internos ---------------------------------------------------------
    def _notify(self, snapshot: JobStatus) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(snapshot)
        except Exception:  # noqa: BLE001 - a UI nunca derruba o motor
            logger.exception("on_update falhou")

    def _update(self, job: _Job, **changes) -> JobStatus:
        with self._lock:
            job.status = replace(job.status, updated_at=now(), **changes)
            snapshot = job.status
            self._notify(snapshot)
        return snapshot

    def _wait_for_live(self, job: _Job) -> bool:
        """True quando está ao vivo; False se pediram para parar. RecorderError sobe."""
        while not job.stop_event.is_set():
            if is_live(job.status.url):
                return True
            deadline = time.monotonic() + config.POLL_INTERVAL_S
            while time.monotonic() < deadline:
                if job.stop_event.wait(_POLL_SLICE_S):
                    return False
        return False

    def _job_main(self, job: _Job) -> None:
        try:
            self._job_loop(job)
        except RecorderError as exc:
            logger.error("Job %s: %s", job.status.job_id, exc)
            self._update(job, state=JobState.ERROR, message=f"Erro: {exc}", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - nunca derruba o manager
            logger.exception("Job %s falhou", job.status.job_id)
            self._update(job, state=JobState.ERROR, message=f"Erro inesperado: {exc}", error=str(exc))

    def _job_loop(self, job: _Job) -> None:
        while True:
            self._update(job, state=JobState.WAITING, message="Aguardando a live começar",
                         recorded_s=0.0, recorded_bytes=0, chat_messages=0)
            if not self._wait_for_live(job):
                self._update(job, state=JobState.STOPPED, message="Parado")
                return

            self._update(job, state=JobState.RECORDING, message="Conectando à live",
                         error=None, session_dir=None, spikes=0, clips_done=0, clips_total=0,
                         final_clips=())

            def on_record(info: dict, _job: _Job = job) -> None:
                changes = {
                    "recorded_s": float(info.get("recorded_s", 0.0)),
                    "recorded_bytes": int(info.get("recorded_bytes", 0)),
                    "chat_messages": int(info.get("chat_messages", 0)),
                }
                if "session_dir" in info:
                    changes["session_dir"] = info["session_dir"]
                changes["message"] = _RECORDING_MESSAGE
                self._update(_job, state=JobState.RECORDING, **changes)

            session = record_session(
                job.status.url, job.stop_event, max_duration=job.max_duration,
                on_progress=on_record, job_tag=job.tag,
            )
            job.session = session
            job.outcome = {}
            job.done_event.clear()
            self._update(job, state=JobState.QUEUED, message="Na fila para gerar clipes",
                         session_dir=session.root)
            self._queue.put(job)
            while not job.done_event.wait(_POLL_SLICE_S):
                if self._shutdown.is_set() and not job.processing:
                    # a thread de clipes pode já ter saído: descarta aqui mesmo
                    logger.info(
                        "Encerrando: clipes de %s não gerados (gravação em %s; use --process)",
                        job.status.channel, session.root,
                    )
                    job.outcome = {"discarded": True}
                    break

            outcome = job.outcome
            if outcome.get("discarded"):
                self._update(
                    job, state=JobState.STOPPED,
                    message="Gravação salva; clipes não gerados (use --process depois)",
                )
                return
            failure = outcome.get("error")
            finals: list[Path] = outcome.get("finals", [])
            if failure:
                logger.error("Job %s: pós-processamento falhou: %s", job.status.job_id, failure)
                if job.once:
                    self._update(job, state=JobState.ERROR, message=f"Erro ao gerar clipes: {failure}",
                                 error=failure)
                    return
                self._update(job, error=failure)
            else:
                if finals:
                    message = f"Pronto: {_fmt_count(len(finals), 'clipe', 'clipes')}"
                else:
                    message = "Nenhum momento de destaque encontrado"
                if job.once or job.stop_event.is_set():
                    self._update(job, state=JobState.DONE, message=message,
                                 final_clips=tuple(finals), error=None)
                    return
                self._update(job, final_clips=tuple(finals), message=message)
            if job.stop_event.is_set():
                self._update(job, state=JobState.STOPPED, message="Parado")
                return
            logger.info("Job %s: aguardando a próxima live", job.status.job_id)

    # --- fila de clipes ----------------------------------------------------
    def _post_loop(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=_POLL_SLICE_S)
            except queue.Empty:
                if self._shutdown.is_set():
                    return
                continue
            if self._shutdown.is_set():
                logger.info(
                    "Encerrando: clipes de %s não gerados (gravação em %s; use --process)",
                    job.status.channel, job.session.root if job.session else "?",
                )
                job.outcome = {"discarded": True}
                job.done_event.set()
                continue
            self._process_job(job)

    def _process_job(self, job: _Job) -> None:
        thread = threading.current_thread()
        original_name = thread.name
        thread.name = f"{job.tag}-post"
        job.processing = True
        try:
            self._update(job, state=JobState.PROCESSING, message="Preparando os clipes",
                         clips_done=0, clips_total=0)

            def on_post(info: dict) -> None:
                done = int(info.get("clips_done", 0))
                total = int(info.get("clips_total", 0))
                if total > 0 and done < total:
                    message = f"Gerando clipe {done + 1} de {total}"
                elif total > 0:
                    message = "Finalizando os clipes"
                else:
                    message = "Procurando os momentos de destaque"
                self._update(job, state=JobState.PROCESSING, message=message,
                             spikes=int(info.get("spikes", job.status.spikes)),
                             clips_done=done, clips_total=total)

            assert job.session is not None
            try:
                finals = process_session(
                    job.session, model_cache=self._model_cache, on_progress=on_post,
                    stop_event=self._shutdown,
                )
                status = job.session.status.get("postprocess", "")
                if status.startswith("erro"):
                    detail = status.partition(":")[2].strip() or status
                    match = re.fullmatch(r"0/(\d+) clipes", detail)
                    if match:
                        detail = f"nenhum dos {match.group(1)} clipes pôde ser gerado"
                    job.outcome = {"error": detail}
                else:
                    job.outcome = {"finals": list(finals)}
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pós-processamento do job %s falhou", job.status.job_id)
                job.outcome = {"error": str(exc)}
        finally:
            thread.name = original_name
            job.processing = False
            job.done_event.set()
