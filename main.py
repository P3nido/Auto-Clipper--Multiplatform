"""Auto-Clipper Multiplataforma — ponto de entrada.

Uso:
    python main.py                      # monitora STREAM_URL para sempre
    python main.py --once               # sai depois de processar uma live
    python main.py --url <URL>          # sobrescreve STREAM_URL
    python main.py --process <pasta>    # refaz só o pós-processamento de uma sessão
    python main.py --max-duration 120   # limita a gravação a N segundos

Pré-requisitos: ver README.md (FFmpeg no PATH; Chrome para Kick).
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import config
from chat_listener import ChatListener, detect_platform, make_chat_source
from clipper import ClipperError, extract_audio, plan_clips, probe_duration, render_clip
from hype_detector import find_spikes
from recorder import Recorder, RecorderError, is_live
from session import Session
from transcriber import load_model, transcribe_to_srt

__all__ = [
    "PrerequisiteError", "main", "monitor_loop", "run_session", "postprocess",
    "setup_logging", "check_prerequisites",
]

logger = logging.getLogger("auto_clipper")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
_HANDLER_TAG = "_auto_clipper_handler"
_POLL_SLICE_S = 0.5
_RECORDER_JOIN_S = 15.0
_CHAT_JOIN_S = 10.0


class PrerequisiteError(RuntimeError):
    """Pré-requisito ausente (ffmpeg, streamlink, whisper) ou URL não suportada."""


class _Formatter(logging.Formatter):
    """O streamlink renomeia os níveis globais para minúsculo ao ser importado;
    normaliza para que o run.log fique consistente (INFO, WARNING, ERROR)."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = record.levelname.upper()
        return super().format(record)


def _tagged(handler: logging.Handler, kind: str) -> logging.Handler:
    setattr(handler, _HANDLER_TAG, kind)
    return handler


def setup_logging(log_file: Path | None = None) -> None:
    """Console + (opcional) arquivo da sessão, no nível config.LOG_LEVEL.

    Idempotente: o handler de console é criado uma vez e o de arquivo é trocado."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO))
    formatter = _Formatter(_LOG_FORMAT)

    if not any(getattr(h, _HANDLER_TAG, None) == "console" for h in root.handlers):
        if sys.platform == "win32":
            for stream in (sys.stdout, sys.stderr):
                reconfigure = getattr(stream, "reconfigure", None)
                if reconfigure is not None:
                    try:
                        reconfigure(encoding="utf-8", errors="replace")
                    except (OSError, ValueError):
                        pass
        console = _tagged(logging.StreamHandler(), "console")
        console.setFormatter(formatter)
        root.addHandler(console)

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, None) == "file":
            root.removeHandler(handler)
            handler.close()
    if log_file is not None:
        file_handler = _tagged(logging.FileHandler(log_file, encoding="utf-8"), "file")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def _resolve_ffmpeg_tools() -> None:
    """Garante ffmpeg/ffprobe; no Windows tenta o link do winget (PATH desatualizado)."""
    for attr, exe in (("FFMPEG_BIN", "ffmpeg.exe"), ("FFPROBE_BIN", "ffprobe.exe")):
        current = getattr(config, attr)
        if shutil.which(current):
            continue
        localappdata = os.environ.get("LOCALAPPDATA")
        if sys.platform == "win32" and localappdata:
            candidate = Path(localappdata) / "Microsoft" / "WinGet" / "Links" / exe
            if candidate.exists():
                logger.info(
                    "%s não está no PATH (o PATH não é atualizado após o winget); usando %s",
                    current, candidate,
                )
                setattr(config, attr, str(candidate))
                continue
        raise PrerequisiteError(
            f"{current} não encontrado. Instale com: winget install Gyan.FFmpeg "
            "(e reabra o terminal) ou ajuste config.FFMPEG_BIN/FFPROBE_BIN"
        )
    # O Whisper chama "ffmpeg" pelo NOME (whisper.audio.load_audio), ignorando
    # config.FFMPEG_BIN: garante que a pasta do ffmpeg resolvido esteja no PATH.
    ffmpeg_dir = str(Path(shutil.which(config.FFMPEG_BIN) or config.FFMPEG_BIN).resolve().parent)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if shutil.which("ffmpeg") is None or os.path.normcase(ffmpeg_dir) not in map(os.path.normcase, path_dirs):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
        logger.debug("Pasta do ffmpeg adicionada ao PATH do processo: %s", ffmpeg_dir)


def check_prerequisites(url: str | None = None) -> None:
    """Falha cedo (PrerequisiteError) se ffmpeg/ffprobe/streamlink/whisper não
    estiverem disponíveis ou se a URL não for de uma plataforma suportada."""
    _resolve_ffmpeg_tools()
    if importlib.util.find_spec("streamlink") is None:
        raise PrerequisiteError("streamlink não instalado. Rode: pip install streamlink")
    target = config.STREAM_URL if url is None else url
    try:
        detect_platform(target)
    except ValueError as exc:
        raise PrerequisiteError(f"URL não suportada ({target}): {exc}") from exc
    engine = config.WHISPER_ENGINE
    if engine == "faster":
        if importlib.util.find_spec("faster_whisper") is None:
            raise PrerequisiteError("faster-whisper não instalado. Rode: pip install faster-whisper")
    elif engine == "openai":
        if importlib.util.find_spec("whisper") is None:
            raise PrerequisiteError("openai-whisper não instalado. Rode: pip install openai-whisper")
    else:
        raise PrerequisiteError(
            f"WHISPER_ENGINE desconhecido: {engine!r} (use 'faster' ou 'openai')"
        )


def run_session(url: str, *, max_duration: float | None = None) -> Session:
    """Grava UMA live: cria a Session, inicia as threads Recorder e ChatListener
    com um stop_event compartilhado, espera o Recorder terminar e faz join no chat
    (timeout 10 s). Ctrl+C: seta stop_event e retorna a sessão mesmo assim.
    max_duration: para a gravação após N segundos."""
    session = Session.create(url, Path(config.SESSIONS_DIR))
    setup_logging(session.log_path)
    logger.info("Sessão iniciada em %s", session.root)
    stop_event = threading.Event()

    recorder = Recorder(session, stop_event)
    rec_thread = threading.Thread(target=recorder.run, name="recorder", daemon=True)

    listener: ChatListener | None = None
    chat_thread: threading.Thread | None = None
    try:
        source = make_chat_source(url)
        listener = ChatListener(session, source, stop_event)
        chat_thread = threading.Thread(target=listener.run, name="chat", daemon=True)
    except Exception:  # noqa: BLE001 - sem chat ainda dá para gravar o vídeo
        logger.exception("Não foi possível criar a fonte de chat; gravando só o vídeo")

    interrupted = False
    started = time.monotonic()
    rec_thread.start()
    if chat_thread is not None:
        chat_thread.start()
    try:
        while rec_thread.is_alive():
            rec_thread.join(_POLL_SLICE_S)
            if max_duration is not None and time.monotonic() - started >= max_duration:
                logger.info("max-duration de %.1f s atingido; encerrando", max_duration)
                break
    except KeyboardInterrupt:
        interrupted = True
        logger.info("interrompido pelo usuário")

    stop_event.set()
    try:
        rec_thread.join(_RECORDER_JOIN_S)
        if chat_thread is not None:
            chat_thread.join(_CHAT_JOIN_S)
    except KeyboardInterrupt:
        interrupted = True
        logger.info("interrompido pelo usuário durante o encerramento")
    if rec_thread.is_alive():
        logger.warning("Recorder não encerrou em %.0f s", _RECORDER_JOIN_S)

    session.status["record"] = "interrompido" if interrupted else "ok"
    session.save()
    total_bytes = sum(seg.path.stat().st_size for seg in session.segments if seg.path.exists())
    logger.info(
        "Gravação encerrada: %d segmento(s), %d bytes, %d mensagens de chat, %d reconexões",
        len(session.segments), total_bytes,
        listener.message_count if listener else 0,
        listener.reconnect_count if listener else 0,
    )
    return session


def _clip_name(index: int, segment_index: int, start_s: float) -> str:
    total = int(start_s)
    return f"clip_{index:02d}_seg{segment_index}_{total // 60:02d}m{total % 60:02d}s"


def postprocess(session: Session) -> list[Path]:
    """Etapas 3 a 5 em sequência. Cada clipe é isolado: a falha de um não para os
    outros. Atualiza session.status. Retorna os caminhos em final/."""
    setup_logging(session.log_path)
    spikes = find_spikes(session)

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
        return []

    try:
        model = load_model()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao carregar o modelo Whisper")
        session.status["postprocess"] = f"erro: {exc}"
        session.save()
        return []

    finals: list[Path] = []
    total = len(specs)
    for i, spec in enumerate(specs, start=1):
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
    if config.DELETE_RAW_AFTER_PROCESS and len(finals) == total:
        for path in sources.values():
            try:
                path.unlink()
                logger.info("Gravação bruta apagada: %s", path)
            except OSError as exc:
                logger.warning("Não foi possível apagar %s: %s", path, exc)
    session.save()
    return finals


def _sleep_interruptible(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_POLL_SLICE_S, remaining))


def monitor_loop(url: str, once: bool = False, max_duration: float | None = None) -> None:
    """A cada POLL_INTERVAL_S: is_live? -> run_session -> postprocess."""
    try:
        while True:
            try:
                live = is_live(url)
            except RecorderError as exc:
                logger.error("%s", exc)
                raise SystemExit(1) from exc
            if live:
                session = run_session(url, max_duration=max_duration)
                postprocess(session)
                if once:
                    return
                logger.info("aguardando próxima live")
            else:
                logger.info("Canal offline; nova checagem em %s s", config.POLL_INTERVAL_S)
            _sleep_interruptible(config.POLL_INTERVAL_S)
    except KeyboardInterrupt:
        logger.info("interrompido pelo usuário; saindo")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Clipper Multiplataforma")
    parser.add_argument("--url", default=config.STREAM_URL, help="URL do canal (Twitch, YouTube ou Kick)")
    parser.add_argument("--once", action="store_true", help="sai depois de processar uma live")
    parser.add_argument("--process", type=Path, metavar="PASTA", help="refaz o pós-processamento de uma sessão")
    parser.add_argument("--max-duration", type=float, default=None, metavar="SEGUNDOS",
                        help="para a gravação após N segundos")
    args = parser.parse_args(argv)
    setup_logging()

    try:
        if args.process is not None:
            try:
                session = Session.load(args.process)
            except (OSError, ValueError, KeyError) as exc:
                logger.error("Não foi possível carregar a sessão %s: %s", args.process, exc)
                return 1
            check_prerequisites(session.url)
            finals = postprocess(session)
            for path in finals:
                logger.info("Clipe final: %s", path)
            logger.info("%d clipe(s) final(is) em %s", len(finals), session.final_dir)
            return 1 if session.status.get("postprocess", "").startswith("erro") else 0
        check_prerequisites(args.url)
    except PrerequisiteError as exc:
        logger.error("%s", exc)
        return 1
    try:
        monitor_loop(args.url, once=args.once, max_duration=args.max_duration)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
