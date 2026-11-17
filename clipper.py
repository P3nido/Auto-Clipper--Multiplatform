"""Etapa 4 — recorte automático dos clipes com FFmpeg.

plan_clips() é PURA (testada). cut_clip()/probe_duration() chamam ffmpeg/ffprobe.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import config
import runtime
import hardware
from hype_detector import Spike

__all__ = [
    "ClipperError", "ClipSpec", "plan_clips", "probe_duration",
    "extract_audio", "render_clip", "cut_clip", "build_video_filter",
]

logger = logging.getLogger(__name__)

# Janelas mais curtas que isso (após o recorte nos limites do vídeo) são descartadas.
MIN_CLIP_S = 3.0

# Caracteres que quebram o parsing do filtro subtitles= do ffmpeg.
_UNSAFE_SRT_CHARS = set("'\":,[];\\%")


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


_VERTICAL_LAYOUTS = ("vertical_blur", "vertical_crop")
_VALID_LAYOUTS = (*_VERTICAL_LAYOUTS, "horizontal")
# O desfoque roda numa versão reduzida (1/_BLUR_DOWNSCALE) do quadro: muito mais barato.
_BLUR_DOWNSCALE = 4
_BLUR_FILTER = "boxblur=10:2"


def build_video_filter(
    layout: str,
    srt_name: str | None,
    *,
    size: tuple[int, int],
    fps: int | None,
    style: str,
) -> str:
    """Monta o filtergraph (-filter_complex) com entrada [0:v] e saída rotulada [v].

    Função PURA. Ordem: fps (se não None) -> layout -> subtitles (se srt_name).
    O fps vem PRIMEIRO para que escala/desfoque/overlay processem só os quadros
    que vão para a saída (metade, numa live de 60 fps).
    Layouts: "vertical_blur", "vertical_crop", "horizontal". Outro valor -> ValueError."""
    if layout not in _VALID_LAYOUTS:
        raise ValueError(f"layout inválido: {layout!r} (use um de {', '.join(_VALID_LAYOUTS)})")
    w, h = size
    steps: list[str] = [f"fps={fps}"] if fps is not None else []

    if layout == "vertical_blur":
        sw, sh = w // _BLUR_DOWNSCALE, h // _BLUR_DOWNSCALE
        steps.append("split=2[bg][fg]")
        head = (
            "[0:v]" + ",".join(steps) + ";"
            f"[bg]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
            f"{_BLUR_FILTER},scale={w}:{h},setsar=1[bgb];"
            f"[fg]scale={w}:-2,setsar=1[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
        steps = []
    else:
        if layout == "vertical_crop":
            steps.append(f"crop=trunc(ih*{w}/{h}/2)*2:ih,scale={w}:{h},setsar=1")
        head = "[0:v]"

    if srt_name:
        extra = f":original_size={w}x{h}" if layout in _VERTICAL_LAYOUTS else ""
        steps.append(f"subtitles={srt_name}{extra}:force_style='{style}'")
    if layout == "vertical_blur":
        return head + ("," + ",".join(steps) if steps else "") + "[v]"
    return head + (",".join(steps) or "null") + "[v]"


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
            creationflags=runtime.subprocess_flags(),
        )
    except FileNotFoundError as exc:
        raise ClipperError(
            f"FFmpeg não encontrado ({cmd[0]}): instale com winget install Gyan.FFmpeg "
            "ou ajuste FFMPEG_BIN/FFPROBE_BIN em config.py"
        ) from exc
    except OSError as exc:
        raise ClipperError(f"não foi possível executar {cmd[0]}: {exc}") from exc


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def probe_duration(path: Path) -> float:
    """Duração em segundos via `ffprobe -v error -show_entries format=duration -of csv=p=0`."""
    cmd = [
        config.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise ClipperError(f"ffprobe falhou para {path}: {_tail(proc.stderr)}")
    try:
        value = float(proc.stdout.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise ClipperError(
            f"ffprobe retornou duração inválida para {path}: {proc.stdout!r} {_tail(proc.stderr)}"
        ) from exc
    if value != value or value < 0:
        raise ClipperError(f"ffprobe retornou duração inválida para {path}: {value}")
    return value




def extract_audio(source: Path, spec: ClipSpec, out_wav: Path) -> Path:
    """Extrai a janela do clipe como WAV mono 16 kHz (entrada ideal do Whisper).
    Fonte sem trilha de áudio -> ClipperError."""
    out_abs = Path(out_wav).resolve()
    out_abs.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{spec.start_s:.3f}", "-i", str(Path(source).resolve()), "-t", f"{spec.duration_s:.3f}",
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_abs),
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        err = _tail(proc.stderr)
        if "matches no streams" in err or "does not contain any stream" in err:
            raise ClipperError(f"sem trilha de áudio em {source}: {err}")
        raise ClipperError(f"ffmpeg falhou ao extrair áudio de {source}: {err}")
    if not out_abs.exists() or out_abs.stat().st_size == 0:
        raise ClipperError(f"ffmpeg não gerou áudio válido em {out_abs}: {_tail(proc.stderr)}")
    return out_abs


def render_clip(source: Path, spec: ClipSpec, out_path: Path, srt_path: Path | None = None) -> Path:
    """Corta e (opcionalmente) queima a legenda em UM único encode.

    Windows: o filtro subtitles exige escapar ':' e '\' no caminho; por isso o ffmpeg
    roda com cwd=srt.parent e recebe só o NOME do .srt (copiado para um nome seguro
    se tiver caracteres problemáticos). srt None ou só whitespace -> sem legenda."""
    src_abs = Path(source).resolve()
    out_abs = Path(out_path).resolve()
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    srt_abs: Path | None = None
    if srt_path is not None:
        candidate = Path(srt_path).resolve()
        if candidate.exists() and candidate.read_text(encoding="utf-8", errors="replace").strip():
            srt_abs = candidate
        else:
            logger.info("SRT vazio ou ausente; renderizando %s sem legenda", out_abs.name)

    tmp_srt: Path | None = None
    cwd: str | None = None
    srt_name: str | None = None
    layout = config.OUTPUT_LAYOUT
    style = config.SUBTITLE_STYLE if layout == "horizontal" else config.SUBTITLE_STYLE_VERTICAL
    if srt_abs is not None:
        srt_name = srt_abs.name
        if any(c in _UNSAFE_SRT_CHARS for c in srt_name) or not srt_name.isascii():
            tmp_srt = srt_abs.parent / f"_burn_{uuid.uuid4().hex[:8]}.srt"
            shutil.copyfile(srt_abs, tmp_srt)
            srt_name = tmp_srt.name
        cwd = str(srt_abs.parent)
    graph = build_video_filter(
        layout, srt_name, size=tuple(config.VERTICAL_SIZE), fps=config.OUTPUT_FPS, style=style,
    )

    def build_cmd(profile: hardware.HardwareProfile) -> list[str]:
        cmd = [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y"]
        cmd += hardware.decoder_args(profile)
        cmd += ["-ss", f"{spec.start_s:.3f}", "-i", str(src_abs), "-t", f"{spec.duration_s:.3f}"]
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?"]
        cmd += hardware.encoder_args(profile)
        cmd += ["-c:a", "aac", "-b:a", config.AUDIO_BITRATE, "-movflags", "+faststart", str(out_abs)]
        return cmd

    def failure(proc: subprocess.CompletedProcess[str]) -> str | None:
        """None se o render deu certo; senão a descrição do problema."""
        if proc.returncode != 0:
            return f"ffmpeg falhou ao renderizar {src_abs}: {_tail(proc.stderr)}"
        if not out_abs.exists() or out_abs.stat().st_size == 0:
            return f"ffmpeg não gerou saída válida em {out_abs}: {_tail(proc.stderr)}"
        return None

    profile = hardware.detect()
    try:
        proc = _run(build_cmd(profile), cwd=cwd)
        problem = failure(proc)
        if problem is not None and (profile.video_encoder != "cpu" or profile.hw_decode):
            logger.warning(
                "Render com GPU (%s, decode %s) falhou; refazendo %s na CPU. Motivo: %s",
                profile.video_encoder, profile.hw_decode or "CPU", out_abs.name, problem,
            )
            cpu_profile = replace(profile, video_encoder="cpu", hw_decode=None)
            proc = _run(build_cmd(cpu_profile), cwd=cwd)
            problem = failure(proc)
    finally:
        if tmp_srt is not None:
            tmp_srt.unlink(missing_ok=True)
    if problem is not None:
        raise ClipperError(problem)
    return out_abs


def cut_clip(source: Path, spec: ClipSpec, out_path: Path) -> Path:
    """Corte preciso sem legenda (compatibilidade): render_clip sem SRT."""
    return render_clip(source, spec, out_path, None)
