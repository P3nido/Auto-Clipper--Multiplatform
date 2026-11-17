"""Etapa 5 — transcrição com Whisper local (faster-whisper ou openai-whisper).

load_model()/transcribe_to_srt() são as ÚNICAS funções que conhecem o motor
de transcrição (ADR-005). A legenda é queimada por clipper.render_clip
(um único encode).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config

__all__ = ["TranscriberError", "load_model", "transcribe_to_srt", "format_srt", "split_into_cues"]

_CUE_PAUSE_S = 0.8  # pausa entre palavras que força nova legenda
_CUE_MIN_DURATION_S = 0.3
_CUE_COMMA_FILL_RATIO = 0.6  # vírgula só quebra com o bloco já >= 60% de max_chars
_CUE_ORPHAN_MAX_CHARS = 3
_CUE_ORPHAN_TOLERANCE = 8
_SENTENCE_END = (".", "!", "?", "…")

logger = logging.getLogger(__name__)


class TranscriberError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Model:
    """Modelo carregado + engine que o criou (transcribe_to_srt despacha por ele)."""
    engine: str
    impl: Any


def load_model(name: str | None = None) -> Any:
    """Carrega o modelo uma vez por pós-processamento, conforme config.WHISPER_ENGINE
    ("faster" = faster-whisper, "openai" = openai-whisper). Retorna um _Model opaco."""
    engine = config.WHISPER_ENGINE
    model_name = name or config.WHISPER_MODEL
    if engine == "faster":
        try:
            from faster_whisper import WhisperModel  # import tardio: pesado
        except ImportError as exc:
            raise TranscriberError(
                "pacote 'faster-whisper' não instalado: rode pip install faster-whisper"
            ) from exc
        try:
            impl = WhisperModel(model_name, device="cpu", compute_type=config.WHISPER_COMPUTE_TYPE)
        except Exception as exc:
            raise TranscriberError(f"falha ao carregar o modelo Whisper {model_name!r}: {exc}") from exc
        return _Model(engine, impl)
    if engine == "openai":
        try:
            import whisper  # import tardio: pesado e opcional nos testes
        except ImportError as exc:
            raise TranscriberError(
                "pacote 'openai-whisper' não instalado: rode pip install openai-whisper"
            ) from exc
        try:
            return _Model(engine, whisper.load_model(model_name))
        except Exception as exc:
            raise TranscriberError(f"falha ao carregar o modelo Whisper {model_name!r}: {exc}") from exc
    raise TranscriberError(
        f"engine não suportada: {engine!r} (use 'faster' ou 'openai' em config.WHISPER_ENGINE)"
    )


def _read_wav_16k_mono(path: Path) -> Any | None:
    """Lê um WAV PCM16 mono 16 kHz (o formato de clipper.extract_audio) como float32.
    Evita a decodificação via PyAV do faster-whisper, que quebra com versões do `av`
    incompatíveis (ex.: av 19 + faster-whisper 1.2.1). Retorna None se não se aplicar."""
    if Path(path).suffix.lower() != ".wav" or not Path(path).is_file():
        return None
    try:
        import wave

        import numpy as np

        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
                return None
            raw = wf.readframes(wf.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    except Exception:  # noqa: BLE001 - cai no caminho normal (arquivo)
        return None


def _fmt_ts(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_srt(segments: list[dict[str, Any]]) -> str:
    """PURA: [{"start": s, "end": e, "text": t}, ...] -> texto SRT (HH:MM:SS,mmm)."""
    blocks: list[str] = []
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}\n{text}\n"
        )
    return "\n".join(blocks)


def _valid_time(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def split_into_cues(words: list[dict[str, Any]], *, max_chars: int, max_duration: float) -> list[dict[str, Any]]:
    """PURA: palavras [{"start","end","word"}] -> blocos curtos [{"start","end","text"}]
    para format_srt. Guloso: quebra por tamanho, duração, pausa e fim de frase."""
    clean: list[tuple[float, float, str]] = []
    for w in words:
        text = " ".join(str(w.get("word", "")).split())
        start, end = w.get("start"), w.get("end")
        if not text or not _valid_time(start) or not _valid_time(end) or end < start:
            continue
        clean.append((float(start), float(end), text))

    blocks: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    cur_len = 0
    for start, end, text in clean:
        if cur:
            if (
                cur_len + 1 + len(text) > max_chars
                or end - cur[0][0] > max_duration
                or start - cur[-1][1] > _CUE_PAUSE_S
            ):
                blocks.append(cur)
                cur, cur_len = [], 0
        cur_len = cur_len + 1 + len(text) if cur else len(text)
        cur.append((start, end, text))
        if text.endswith(_SENTENCE_END) or (
            text.endswith(",") and cur_len >= _CUE_COMMA_FILL_RATIO * max_chars
        ):
            blocks.append(cur)
            cur, cur_len = [], 0
    if cur:
        blocks.append(cur)

    # bloco órfão (1 palavra curta, ex. "né?") em QUALQUER posição é juntado ao
    # anterior se couber e não vier depois de uma pausa: evita legenda-relâmpago.
    merged: list[list[tuple[float, float, str]]] = []
    for block in blocks:
        if (
            merged
            and len(block) == 1
            and len(block[0][2]) <= _CUE_ORPHAN_MAX_CHARS
            and block[0][0] - merged[-1][-1][1] <= _CUE_PAUSE_S
            and len(" ".join(t for _, _, t in merged[-1])) + 1 + len(block[0][2])
            <= max_chars + _CUE_ORPHAN_TOLERANCE
        ):
            merged[-1].extend(block)
        else:
            merged.append(block)
    blocks = merged

    cues: list[dict[str, Any]] = [
        {"start": b[0][0], "end": b[-1][1], "text": " ".join(t for _, _, t in b)} for b in blocks
    ]
    for i, cue in enumerate(cues):
        cue["end"] = max(cue["end"], cue["start"] + _CUE_MIN_DURATION_S)
        if i + 1 < len(cues):
            cue["end"] = min(cue["end"], cues[i + 1]["start"])
    return cues


def _norm_words(raw: Any) -> list[dict[str, Any]]:
    """Normaliza palavras (objetos do faster-whisper ou dicts do openai) para dicts."""
    out: list[dict[str, Any]] = []
    for w in raw or []:
        if isinstance(w, dict):
            out.append({"start": w.get("start"), "end": w.get("end"), "word": w.get("word", "")})
        else:
            out.append({"start": getattr(w, "start", None), "end": getattr(w, "end", None),
                        "word": getattr(w, "word", "")})
    return out


def transcribe_to_srt(model: Any, media_path: Path, out_srt: Path, language: str | None = None) -> Path:
    """Transcreve áudio/vídeo e grava o SRT em UTF-8. Sem fala = SRT vazio (não é erro).
    language None -> config.WHISPER_LANGUAGE (None = detecção automática)."""
    lang = language if language is not None else config.WHISPER_LANGUAGE
    if not isinstance(model, _Model):  # aceita um modelo openai cru (compatibilidade)
        model = _Model("openai", model)
    try:
        if model.engine == "faster":
            audio = _read_wav_16k_mono(media_path)
            segments, info = model.impl.transcribe(
                audio if audio is not None else str(media_path), language=lang, vad_filter=config.WHISPER_VAD, beam_size=5,
                word_timestamps=True,
            )
            # o gerador é lazy: precisa ser consumido dentro do try
            items = []
            words: list[dict[str, Any]] = []
            for s in segments:
                items.append({"start": s.start, "end": s.end, "text": s.text})
                words.extend(_norm_words(getattr(s, "words", None)))
            logger.debug(
                "idioma detectado: %s (p=%.2f)",
                getattr(info, "language", None),
                float(getattr(info, "language_probability", 0.0) or 0.0),
            )
        else:
            result = model.impl.transcribe(str(media_path), language=lang, fp16=False, verbose=None,
                                         word_timestamps=True)
            items = result.get("segments", [])
            words = []
            for seg in items:
                words.extend(_norm_words(seg.get("words")))
        if config.SUBTITLE_MAX_CHARS > 0 and words:
            items = split_into_cues(
                words, max_chars=config.SUBTITLE_MAX_CHARS, max_duration=config.SUBTITLE_MAX_CUE_S
            )
        srt = format_srt(items)
    except Exception as exc:
        raise TranscriberError(f"falha ao transcrever {media_path}: {exc}") from exc
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    out_srt.write_text(srt, encoding="utf-8")
    return out_srt
