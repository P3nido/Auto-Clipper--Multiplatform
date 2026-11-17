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

# Janelas mais curtas que isso (após o recorte nos limites do vídeo) são descartadas.
MIN_CLIP_S = 3.0


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

    Janelas que apenas se tocam (fim == início) também são fundidas. Picos cujo
    segment_index não está em segment_durations são ignorados (warning).
    Empate de peak_count na seleção: vence o mais cedo. max_clips < 0 = sem limite.
    """
    by_segment: dict[int, list[tuple[float, float, int]]] = {}
    for sp in spikes:
        duration = segment_durations.get(sp.segment_index)
        if duration is None:
            logger.warning("Pico ignorado: segmento %d sem duração conhecida", sp.segment_index)
            continue
        start = max(0.0, sp.video_offset_s - pre_s)
        end = min(duration, sp.video_offset_s + post_s)
        if end <= start:
            continue
        by_segment.setdefault(sp.segment_index, []).append((start, end, sp.count))

    clips: list[ClipSpec] = []
    for seg_index, windows in by_segment.items():
        windows.sort(key=lambda w: (w[0], w[1]))
        cur_start, cur_end, cur_peak = windows[0]
        merged: list[tuple[float, float, int]] = []
        for start, end, peak in windows[1:]:
            if start <= cur_end:
                cur_end = max(cur_end, end)
                cur_peak = max(cur_peak, peak)
            else:
                merged.append((cur_start, cur_end, cur_peak))
                cur_start, cur_end, cur_peak = start, end, peak
        merged.append((cur_start, cur_end, cur_peak))
        for start, end, peak in merged:
            if end - start < MIN_CLIP_S:
                continue
            clips.append(ClipSpec(seg_index, start, end - start, peak))

    clips.sort(key=lambda c: (c.segment_index, c.start_s))
    if max_clips >= 0 and len(clips) > max_clips:
        ranked = sorted(clips, key=lambda c: (-c.peak_count, c.segment_index, c.start_s))
        clips = sorted(ranked[:max_clips], key=lambda c: (c.segment_index, c.start_s))
    return clips


def probe_duration(path: Path) -> float:
    """Duração em segundos via `ffprobe -v error -show_entries format=duration -of csv=p=0`."""
    raise NotImplementedError


def cut_clip(source: Path, spec: ClipSpec, out_path: Path) -> Path:
    """ffmpeg -y -ss <start> -i <source> -t <dur> -c:v libx264 -crf <VIDEO_CRF>
    -preset veryfast -c:a aac -b:a 160k -movflags +faststart <out>.
    Re-encode = corte preciso no frame (essencial para a legenda alinhar)."""
    raise NotImplementedError
