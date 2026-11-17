"""Etapa 3 — detecção de picos de hype no chat.

bucketize() e detect_spike_buckets() são PURAS (sem I/O) e cobertas por testes.
find_spikes() é a única função que lê disco: junta chat.jsonl e os segmentos
da sessão e devolve picos já convertidos para o tempo do vídeo.
Algoritmo detalhado: ARCHITECTURE.md §5.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import config
from session import Session

__all__ = ["Spike", "bucketize", "detect_spike_buckets", "find_spikes"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spike:
    segment_index: int      # qual live_NN.ts
    video_offset_s: float   # instante do pico dentro do arquivo (centro do bloco)
    count: int              # mensagens no bloco
    baseline: float         # média móvel no momento


def bucketize(timestamps: Sequence[float], start: float, end: float, bucket_s: float) -> list[int]:
    """Conta mensagens por bloco contíguo de bucket_s segundos em [start, end).

    Retorna ceil((end - start) / bucket_s) contagens; blocos vazios = 0.
    Timestamps fora do intervalo são ignorados.
    """
    if bucket_s <= 0:
        raise ValueError(f"bucket_s precisa ser > 0 (recebido {bucket_s})")
    n = max(0, math.ceil((end - start) / bucket_s))
    counts = [0] * n
    for t in timestamps:
        if t < start or t >= end:
            continue
        idx = int((t - start) // bucket_s)
        if idx >= n:  # proteção contra erro de ponto flutuante
            idx = n - 1
        counts[idx] += 1
    return counts


def detect_spike_buckets(
    counts: Sequence[int],
    *,
    bucket_s: float,
    multiplier: float,
    baseline_window: int,
    warmup: int,
    min_messages: int,
    cooldown_s: float,
) -> list[tuple[int, float]]:
    """Retorna [(indice_do_bloco, baseline)] dos picos, em ordem crescente.

    Bloco i é candidato se:
        i >= warmup
        and counts[i] >= min_messages
        and counts[i] >= multiplier * baseline_i
    onde baseline_i = média de counts[max(0, i - baseline_window) : i]
    (bloco atual EXCLUÍDO; se baseline_i == 0 vale só o piso min_messages).

    Cooldown: candidatos a menos de cooldown_s de um pico aceito formam a mesma
    rajada; fica apenas o de maior contagem (empate: o primeiro).

    Referência da rajada: um candidato pertence à rajada atual se a distância
    até o candidato ANTERIOR da rajada ((j - i) * bucket_s) for < cooldown_s.
    Assim uma rajada longa e contínua vira um único pico (a rajada "encadeia").
    """
    peaks: list[tuple[int, float]] = []
    burst_best: tuple[int, float] | None = None  # (índice, baseline) do melhor da rajada
    last_candidate = -1
    for i, count in enumerate(counts):
        if i < warmup or count < min_messages:
            continue
        window = counts[max(0, i - baseline_window):i]
        baseline = float(sum(window)) / len(window) if window else 0.0
        if count < multiplier * baseline:
            continue
        if burst_best is not None and (i - last_candidate) * bucket_s < cooldown_s:
            if count > counts[burst_best[0]]:  # estritamente maior: empate fica com o primeiro
                burst_best = (i, baseline)
        else:
            if burst_best is not None:
                peaks.append(burst_best)
            burst_best = (i, baseline)
        last_candidate = i
    if burst_best is not None:
        peaks.append(burst_best)
    return peaks


def _read_chat_timestamps(path: Path) -> list[float]:
    """Lê wall_ts de cada linha válida de chat.jsonl (inválidas são puladas)."""
    timestamps: list[float] = []
    invalid = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                ts = float(data["wall_ts"])
                if not math.isfinite(ts):
                    raise ValueError("wall_ts não finito")
            except (ValueError, KeyError, TypeError):
                invalid += 1
                continue
            timestamps.append(ts)
    if invalid:
        logger.warning("chat.jsonl: %d linha(s) inválida(s) puladas", invalid)
    return timestamps


def find_spikes(session: Session) -> list[Spike]:
    """Lê session.chat_path, aplica CHAT_DELAY_OFFSET_S, separa as mensagens por
    segmento, roda bucketize + detect_spike_buckets com os valores de config e
    converte cada pico com segment.wall_to_offset() (picos em anúncio são
    descartados). Grava session.spikes_path e retorna a lista."""
    spikes: list[Spike] = []
    records: list[dict] = []
    if not session.chat_path.exists():
        logger.warning("Sem chat.jsonl em %s: nenhum pico detectado", session.root)
    else:
        adjusted = [t - config.CHAT_DELAY_OFFSET_S for t in _read_chat_timestamps(session.chat_path)]
        bucket_s = config.BUCKET_SECONDS
        for seg in session.segments:
            if seg.wall_start is None:
                logger.debug("Segmento %d sem wall_start: ignorado", seg.index)
                continue
            end = seg.wall_end
            if end is None:
                later = [t for t in adjusted if t >= seg.wall_start]
                end = max(later) if later else seg.wall_start
            in_seg = [t for t in adjusted if seg.wall_start <= t < end]
            counts = bucketize(in_seg, seg.wall_start, end, bucket_s)
            peaks = detect_spike_buckets(
                counts,
                bucket_s=bucket_s,
                multiplier=config.SPIKE_MULTIPLIER,
                baseline_window=config.BASELINE_WINDOW_BUCKETS,
                warmup=config.WARMUP_BUCKETS,
                min_messages=config.MIN_MESSAGES_PER_BUCKET,
                cooldown_s=config.SPIKE_COOLDOWN_S,
            )
            for idx, baseline in peaks:
                center = seg.wall_start + (idx + 0.5) * bucket_s
                offset = seg.wall_to_offset(center)
                if offset is None:
                    logger.debug("Pico no bloco %d do segmento %d descartado (anúncio/fora)", idx, seg.index)
                    continue
                spikes.append(Spike(seg.index, offset, counts[idx], baseline))
                records.append({
                    "segment_index": seg.index,
                    "video_offset_s": offset,
                    "count": counts[idx],
                    "baseline": baseline,
                    "wall_ts": center,
                })
    session.spikes_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("%d pico(s) detectado(s)", len(spikes))
    return spikes
