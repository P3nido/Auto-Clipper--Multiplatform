"""Etapa 5 — transcrição com Whisper local e legenda queimada com FFmpeg.

load_model()/transcribe_to_srt() são as ÚNICAS funções que conhecem o motor
de transcrição (ADR-005): trocar para faster-whisper mexe só nelas.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import config

__all__ = ["TranscriberError", "load_model", "transcribe_to_srt", "format_srt", "burn_subtitles"]

logger = logging.getLogger(__name__)


class TranscriberError(RuntimeError):
    pass


def load_model(name: str | None = None) -> Any:
    """Carrega o modelo uma vez por pós-processamento (whisper.load_model)."""
    raise NotImplementedError


def format_srt(segments: list[dict[str, Any]]) -> str:
    """PURA: [{"start": s, "end": e, "text": t}, ...] -> texto SRT (HH:MM:SS,mmm)."""
    raise NotImplementedError


def transcribe_to_srt(model: Any, clip_path: Path, out_srt: Path, language: str | None = None) -> Path:
    """model.transcribe(str(clip), language=..., fp16=False) -> format_srt -> grava UTF-8.
    Clipe sem fala gera SRT vazio (não é erro)."""
    raise NotImplementedError


def burn_subtitles(clip_path: Path, srt_path: Path, out_path: Path) -> Path:
    """ffmpeg -y -i <clip> -vf "subtitles=<srt>:force_style='<SUBTITLE_STYLE>'"
    -c:v libx264 -crf <VIDEO_CRF> -preset veryfast -c:a copy <out>.

    Windows: o filtro subtitles exige escapar ':' e '\\' no caminho. Para evitar
    isso, rode o ffmpeg com cwd=srt_path.parent e passe só o NOME do .srt.
    SRT vazio: copia o clipe sem legenda."""
    raise NotImplementedError
