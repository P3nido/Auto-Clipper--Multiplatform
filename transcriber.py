"""Etapa 5 — transcrição com Whisper local (faster-whisper ou openai-whisper).

load_model()/transcribe_to_srt() são as ÚNICAS funções que conhecem o motor
de transcrição (ADR-005). A legenda é queimada por clipper.render_clip
(um único encode).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config

__all__ = ["TranscriberError", "load_model", "transcribe_to_srt", "format_srt"]

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
                audio if audio is not None else str(media_path), language=lang, vad_filter=config.WHISPER_VAD, beam_size=5
            )
            # o gerador é lazy: precisa ser consumido dentro do try
            items = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
            logger.debug(
                "idioma detectado: %s (p=%.2f)",
                getattr(info, "language", None),
                float(getattr(info, "language_probability", 0.0) or 0.0),
            )
        else:
            result = model.impl.transcribe(str(media_path), language=lang, fp16=False, verbose=None)
            items = result.get("segments", [])
        srt = format_srt(items)
    except Exception as exc:
        raise TranscriberError(f"falha ao transcrever {media_path}: {exc}") from exc
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    out_srt.write_text(srt, encoding="utf-8")
    return out_srt
