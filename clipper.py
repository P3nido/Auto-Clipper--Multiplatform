"""Etapa 4 — recorte automático dos clipes com FFmpeg.

plan_clips() é PURA (testada). cut_clip()/probe_duration() chamam ffmpeg/ffprobe.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import config
from hype_detector import Spike

__all__ = ["ClipperError", "ClipSpec", "plan_clips", "probe_duration", "cut_clip"]

logger = logging.getLogger(__name__)


class ClipperError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClipSpec:
    segment_index: int
    start_s: float
    duration_s: float
    peak_count: int   # contagem do pico mais forte da janela (usada para ranquear)


def plan_clips(
    spikes: Sequence[Spike],
    segment_durations: dict[int, float],
    *,
    pre_s: float,
    post_s: float,
    max_clips: int,
) -> list[ClipSpec]:
    """Converte picos em janelas [pico - pre_s, pico + post_s].

    - Limita a [0, duração do segmento].
    - Funde janelas sobrepostas do MESMO segmento (peak_count = máximo).
    - Descarta janelas com menos de 3 s.
    - Se passar de max_clips, mantém as de maior peak_count.
    - Retorna em ordem cronológica (segment_index, start_s).
    """
    raise NotImplementedError


def probe_duration(path: Path) -> float:
    """Duração em segundos via `ffprobe -v error -show_entries format=duration -of csv=p=0`."""
    raise NotImplementedError


def cut_clip(source: Path, spec: ClipSpec, out_path: Path) -> Path:
    """ffmpeg -y -ss <start> -i <source> -t <dur> -c:v libx264 -crf <VIDEO_CRF>
    -preset veryfast -c:a aac -b:a 160k -movflags +faststart <out>.
    Re-encode = corte preciso no frame (essencial para a legenda alinhar)."""
    raise NotImplementedError
