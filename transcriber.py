"""Etapa 5 — transcrição com Whisper local e legenda queimada com FFmpeg.

load_model()/transcribe_to_srt() são as ÚNICAS funções que conhecem o motor
de transcrição (ADR-005): trocar para faster-whisper mexe só nelas.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import config

__all__ = ["TranscriberError", "load_model", "transcribe_to_srt", "format_srt", "burn_subtitles"]

logger = logging.getLogger(__name__)


class TranscriberError(RuntimeError):
    pass


_UNSAFE_SRT_CHARS = set("'\":,[];\\%")


def load_model(name: str | None = None) -> Any:
    """Carrega o modelo uma vez por pós-processamento (whisper.load_model)."""
    if config.WHISPER_ENGINE != "openai":
        raise TranscriberError(
            f"engine não suportada: {config.WHISPER_ENGINE!r} (use 'openai' em config.WHISPER_ENGINE)"
        )
    try:
        import whisper  # import tardio: pesado e opcional nos testes
    except ImportError as exc:
        raise TranscriberError(
            "pacote 'openai-whisper' não instalado: rode pip install openai-whisper"
        ) from exc
    model_name = name or config.WHISPER_MODEL
    try:
        return whisper.load_model(model_name)
    except Exception as exc:
        raise TranscriberError(f"falha ao carregar o modelo Whisper {model_name!r}: {exc}") from exc


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


def transcribe_to_srt(model: Any, clip_path: Path, out_srt: Path, language: str | None = None) -> Path:
    """model.transcribe(str(clip), language=..., fp16=False) -> format_srt -> grava UTF-8.
    Clipe sem fala gera SRT vazio (não é erro)."""
    try:
        result = model.transcribe(
            str(clip_path), language=language or config.WHISPER_LANGUAGE, fp16=False, verbose=None
        )
        srt = format_srt(result.get("segments", []))
    except Exception as exc:
        raise TranscriberError(f"falha ao transcrever {clip_path}: {exc}") from exc
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    out_srt.write_text(srt, encoding="utf-8")
    return out_srt


def burn_subtitles(clip_path: Path, srt_path: Path, out_path: Path) -> Path:
    """ffmpeg -y -i <clip> -vf "subtitles=<srt>:force_style='<SUBTITLE_STYLE>'"
    -c:v libx264 -crf <VIDEO_CRF> -preset <VIDEO_PRESET> -c:a copy <out>.

    Windows: o filtro subtitles exige escapar ':' e '\\' no caminho. Para evitar
    isso, rode o ffmpeg com cwd=srt_path.parent e passe só o NOME do .srt.
    SRT vazio: copia o clipe sem legenda."""
    clip_abs = Path(clip_path).resolve()
    srt_abs = Path(srt_path).resolve()
    out_abs = Path(out_path).resolve()
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    if not srt_abs.read_text(encoding="utf-8", errors="replace").strip():
        logger.info("SRT vazio; copiando %s sem legenda", clip_abs.name)
        shutil.copyfile(clip_abs, out_abs)
        return out_abs

    tmp_srt: Path | None = None
    srt_name = srt_abs.name
    if any(c in _UNSAFE_SRT_CHARS for c in srt_name) or not srt_name.isascii():
        tmp_srt = srt_abs.parent / f"_burn_{uuid.uuid4().hex[:8]}.srt"
        shutil.copyfile(srt_abs, tmp_srt)
        srt_name = tmp_srt.name

    cmd = [
        config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip_abs),
        "-vf", f"subtitles={srt_name}:force_style='{config.SUBTITLE_STYLE}'",
        "-c:v", "libx264", "-crf", str(config.VIDEO_CRF), "-preset", config.VIDEO_PRESET,
        "-c:a", "copy", "-movflags", "+faststart", str(out_abs),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(srt_abs.parent),
        )
    except FileNotFoundError as exc:
        raise TranscriberError(
            "FFmpeg não encontrado: instale com winget install Gyan.FFmpeg "
            "ou ajuste FFMPEG_BIN em config.py"
        ) from exc
    except OSError as exc:
        raise TranscriberError(f"não foi possível executar {config.FFMPEG_BIN}: {exc}") from exc
    finally:
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise TranscriberError(f"ffmpeg falhou ao queimar legenda em {clip_abs}: {tail}")
    if not out_abs.exists() or out_abs.stat().st_size == 0:
        raise TranscriberError(f"ffmpeg não gerou saída válida em {out_abs}")
    return out_abs
