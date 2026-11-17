"""Auto-Clipper Multiplataforma — ponto de entrada.

Uso:
    python main.py                      # monitora STREAM_URL para sempre
    python main.py --once               # sai depois de processar uma live
    python main.py --url <URL>          # sobrescreve STREAM_URL
    python main.py --process <pasta>    # refaz só o pós-processamento de uma sessão

Pré-requisitos: ver README.md (FFmpeg no PATH; Chrome para Kick).
"""
from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path

import config
from chat_listener import ChatListener, make_chat_source
from clipper import cut_clip, plan_clips, probe_duration
from hype_detector import find_spikes
from recorder import Recorder, is_live
from session import Session
from transcriber import burn_subtitles, load_model, transcribe_to_srt

__all__ = ["main", "monitor_loop", "run_session", "postprocess"]

logger = logging.getLogger("auto_clipper")


def setup_logging(log_file: Path | None = None) -> None:
    """Console + (opcional) arquivo da sessão, no nível config.LOG_LEVEL."""
    raise NotImplementedError


def check_prerequisites() -> None:
    """Falha cedo com mensagem clara se ffmpeg/ffprobe/streamlink não estiverem
    disponíveis ou se a URL não for de uma plataforma suportada."""
    raise NotImplementedError


def run_session(url: str) -> Session:
    """Grava UMA live: cria a Session, inicia as threads Recorder e ChatListener
    com um stop_event compartilhado, espera o Recorder terminar e faz join no chat
    (timeout 10 s). Ctrl+C: seta stop_event e retorna a sessão mesmo assim."""
    raise NotImplementedError


def postprocess(session: Session) -> list[Path]:
    """Etapas 3 a 5 em sequência. Cada clipe é isolado: a falha de um não para os
    outros. Atualiza session.status. Retorna os caminhos em final/."""
    raise NotImplementedError


def monitor_loop(url: str, once: bool = False) -> None:
    """A cada POLL_INTERVAL_S: is_live? -> run_session -> postprocess."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Clipper Multiplataforma")
    parser.add_argument("--url", default=config.STREAM_URL, help="URL do canal (Twitch, YouTube ou Kick)")
    parser.add_argument("--once", action="store_true", help="sai depois de processar uma live")
    parser.add_argument("--process", type=Path, metavar="PASTA", help="refaz o pós-processamento de uma sessão")
    args = parser.parse_args(argv)
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
