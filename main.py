"""Auto-Clipper Multiplataforma — ponto de entrada.

Uso:
    python main.py                      # monitora STREAM_URL para sempre
    python main.py --once               # sai depois de processar uma live
    python main.py --url <URL>          # sobrescreve STREAM_URL
    python main.py --url <A> --url <B>  # várias lives ao mesmo tempo
    python main.py --process <pasta>    # refaz só o pós-processamento de uma sessão
    python main.py --max-duration 120   # limita a gravação a N segundos
    python main.py --diagnostico        # mostra a GPU/encoder/Whisper que serão usados

A lógica de gravação e de clipes mora no engine.py; aqui fica só a CLI.
Pré-requisitos: ver README.md (FFmpeg no PATH; Chrome para Kick).
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import config
import hardware
import runtime
from chat_listener import detect_platform
from engine import (_LOG_FORMAT, JobManager, JobState, JobStatus, _fmt_duration, _fmt_size,
                    _LogFormatter, process_session)
from session import Session

__all__ = [
    "PrerequisiteError", "main", "apply_frozen_defaults", "run_jobs", "setup_logging", "check_prerequisites",
]

logger = logging.getLogger("auto_clipper")

_HANDLER_TAG = "_auto_clipper_handler"
_POLL_SLICE_S = 0.5
_RECORDING_LOG_INTERVAL_S = 30.0   # a mensagem "Gravando há ..." muda a cada 2 s; loga só a cada 30 s
_TERMINAL = (JobState.DONE, JobState.STOPPED, JobState.ERROR)


class PrerequisiteError(RuntimeError):
    """Pré-requisito ausente (ffmpeg, streamlink, whisper) ou URL não suportada."""


_Formatter = _LogFormatter


def _tagged(handler: logging.Handler, kind: str) -> logging.Handler:
    setattr(handler, _HANDLER_TAG, kind)
    return handler


def setup_logging(log_file: Path | None = None) -> None:
    """Console + (opcional) arquivo global, no nível config.LOG_LEVEL.

    Idempotente: o handler de console é criado uma vez e o de arquivo é trocado.
    O run.log de cada live é gerenciado pelo engine (um handler por sessão)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO))
    formatter = _Formatter(_LOG_FORMAT)

    if not any(getattr(h, _HANDLER_TAG, None) == "console" for h in root.handlers):
        if sys.platform == "win32":
            for stream in (sys.stdout, sys.stderr):
                if stream is None:
                    continue
                reconfigure = getattr(stream, "reconfigure", None)
                if reconfigure is not None:
                    try:
                        reconfigure(encoding="utf-8", errors="replace")
                    except (OSError, ValueError):
                        pass
        if sys.stderr is not None:  # app "windowed": sem console
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


def apply_frozen_defaults() -> None:
    """Empacotado: a pasta padrão das gravações sai da pasta do programa e vai para Vídeos."""
    if runtime.is_frozen() and config.SESSIONS_DIR == "sessions":
        config.SESSIONS_DIR = runtime.default_sessions_dir()


def _resolve_ffmpeg_tools() -> None:
    """Garante ffmpeg/ffprobe; no Windows tenta o link do winget (PATH desatualizado)."""
    for attr, exe in (("FFMPEG_BIN", "ffmpeg.exe"), ("FFPROBE_BIN", "ffprobe.exe")):
        current = getattr(config, attr)
        bundled = runtime.bundled_tool(exe)
        if bundled:
            setattr(config, attr, bundled)
            continue
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
    estiverem disponíveis ou se a URL não for de uma plataforma suportada.
    Também detecta o hardware (GPU) e registra o perfil escolhido no log."""
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
    # Detecta (e cacheia) o hardware cedo: o perfil aparece no log antes de gravar.
    profile = hardware.detect()
    for note in profile.notes:
        logger.info("Hardware: %s", note)


class _ProgressLogger:
    """Loga o andamento dos jobs sem spam: só quando o estado ou a mensagem mudam
    (e, durante a gravação, no máximo a cada _RECORDING_LOG_INTERVAL_S)."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[JobState, str, float]] = {}

    def __call__(self, status: JobStatus) -> None:
        last = self._last.get(status.job_id)
        stamp = time.monotonic()
        recording = status.state == JobState.RECORDING
        if last is not None:
            if recording and last[0] == JobState.RECORDING:
                if stamp - last[2] < _RECORDING_LOG_INTERVAL_S:
                    return
            elif last[0] == status.state and last[1] == status.message:
                return
        self._last[status.job_id] = (status.state, status.message, stamp)
        level = logging.ERROR if status.state == JobState.ERROR else logging.INFO
        line = status.message
        if recording and status.recorded_bytes:
            line += (f" ({_fmt_duration(status.recorded_s)}, {_fmt_size(status.recorded_bytes)}, "
                     f"{status.chat_messages} msgs)")
        logger.log(level, "[%s] %s: %s", status.channel, status.state.value, line)


def run_jobs(urls: list[str], *, once: bool, max_duration: float | None = None) -> int:
    """Cria um job por URL e espera todos terminarem. Ctrl+C: para todos (a gravação
    em andamento segue para a fila de clipes) e espera a fila; um segundo Ctrl+C
    encerra de vez. Retorna o código de saída (1 se algum job terminou em erro)."""
    manager = JobManager(on_update=_ProgressLogger())
    job_ids: list[str] = []

    def running() -> bool:
        return any(manager.status(j).state not in _TERMINAL for j in job_ids)

    try:
        for url in urls:
            try:
                job_ids.append(manager.add(url, once=once, max_duration=max_duration))
            except (ValueError, RuntimeError) as exc:
                logger.error("%s", exc)
                for job_id in job_ids:
                    manager.stop(job_id)
                return 1
        try:
            while running():
                time.sleep(_POLL_SLICE_S)
        except KeyboardInterrupt:
            logger.info(
                "interrompido pelo usuário; finalizando gravações e clipes "
                "(Ctrl+C de novo para sair já)"
            )
            for job_id in job_ids:
                manager.stop(job_id)
            try:
                while running():
                    time.sleep(_POLL_SLICE_S)
            except KeyboardInterrupt:
                logger.info(
                    "saindo; as gravações brutas ficam em %s (use --process)", config.SESSIONS_DIR
                )
    finally:
        manager.shutdown(wait=True)
    failed = any(manager.status(j).state == JobState.ERROR for j in job_ids)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Clipper Multiplataforma")
    parser.add_argument("--url", action="append", default=None,
                        help="URL do canal (Twitch, YouTube ou Kick); repita para várias lives")
    parser.add_argument("--once", action="store_true", help="sai depois de processar uma live")
    parser.add_argument("--process", type=Path, metavar="PASTA", help="refaz o pós-processamento de uma sessão")
    parser.add_argument("--max-duration", type=float, default=None, metavar="SEGUNDOS",
                        help="para a gravação após N segundos")
    parser.add_argument("--diagnostico", action="store_true",
                        help="mostra o hardware detectado (GPU/encoder/Whisper) e um mini benchmark")
    args = parser.parse_args(argv)
    urls: list[str] = args.url or [config.STREAM_URL]
    apply_frozen_defaults()
    setup_logging()
    runtime.kill_children_on_exit()

    if args.diagnostico:
        try:
            _resolve_ffmpeg_tools()
        except PrerequisiteError as exc:
            logger.error("%s", exc)
            return 1
        print(hardware.diagnose())  # única exceção ao "sem print": é a saída do comando
        return 0

    try:
        if args.process is not None:
            try:
                session = Session.load(args.process)
            except (OSError, ValueError, KeyError) as exc:
                logger.error("Não foi possível carregar a sessão %s: %s", args.process, exc)
                return 1
            check_prerequisites(session.url)
            finals = process_session(session)
            for path in finals:
                logger.info("Clipe final: %s", path)
            logger.info("%d clipe(s) final(is) em %s", len(finals), session.final_dir)
            return 1 if session.status.get("postprocess", "").startswith("erro") else 0
        for url in urls:
            check_prerequisites(url)
    except PrerequisiteError as exc:
        logger.error("%s", exc)
        return 1
    return run_jobs(urls, once=args.once, max_duration=args.max_duration)


if __name__ == "__main__":
    raise SystemExit(main())
