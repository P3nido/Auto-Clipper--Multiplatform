"""Detecção de hardware para aceleração por GPU (ADR-007).

detect() roda UMA vez por processo (cache) e devolve um HardwareProfile, usado
pelo clipper (encode/decode de vídeo) e pelo transcriber (Whisper). Nada aqui
deve derrubar o programa: na dúvida, o perfil cai para CPU.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import config

__all__ = [
    "HardwareProfile",
    "detect",
    "encoder_args",
    "decoder_args",
    "prepare_cuda_dlls",
    "diagnose",
]

logger = logging.getLogger(__name__)

_TEST_TIMEOUT_S = 15.0
_QUERY_TIMEOUT_S = 8.0
_BENCH_TIMEOUT_S = 120.0
_BENCH_SECONDS = 5
# O CRF do x264 e o CQ/QP dos encoders de GPU não são equivalentes: +3 aproxima
# a mesma qualidade visual (GPUs comprimem um pouco menos por bit).
_GPU_QUALITY_OFFSET = 3

_ENCODER_CODECS = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"}
_AUTO_ENCODER_ORDER = ("nvenc", "qsv", "amf")
_VALID_ENCODERS = ("nvenc", "qsv", "amf", "cpu")
_VALID_DECODERS = ("cuda", "d3d11va", "qsv")
_DEFAULT_DECODE = {"nvenc": "cuda", "qsv": "d3d11va", "amf": "d3d11va"}
_AUTO_MODEL_CUDA = "large-v3-turbo"
_AUTO_MODEL_CPU = "small"
_AUTO_MODEL_CUDA_MIN_VRAM_MIB = 6000   # RTX 5060 (8 GB) usa large-v3-turbo; MX450 (2 GB) fica no small

_cache_lock = threading.Lock()
_cached: HardwareProfile | None = None

_dll_lock = threading.Lock()
_dll_handles: list[object] = []       # mantém os handles vivos (senão o Windows remove o diretório)
_dll_dirs_done: set[str] = set()


@dataclass(frozen=True)
class HardwareProfile:
    video_encoder: str                 # "nvenc" | "qsv" | "amf" | "cpu"
    hw_decode: str | None              # "cuda" | "d3d11va" | "qsv" | None
    whisper_device: str                # "cuda" | "cpu"
    whisper_compute_type: str          # "float16" (cuda) | "int8" (cpu) | valor forçado no config
    whisper_model: str                 # nome resolvido (config "auto" -> large-v3-turbo em cuda, small em cpu)
    gpu_names: tuple[str, ...] = ()    # nomes das GPUs encontradas (informativo)
    notes: tuple[str, ...] = field(default_factory=tuple)  # por que cada escolha foi feita (pt-BR)


# --------------------------------------------------------------------------
# DLLs CUDA (cuBLAS/cuDNN instaladas via pip)
# --------------------------------------------------------------------------
def _nvidia_bin_dirs() -> list[Path]:
    roots: list[Path] = []
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        roots.extend(Path(p) for p in spec.submodule_search_locations)
    prefix = Path(sys.prefix)
    roots.append(prefix / "Lib" / "site-packages" / "nvidia")
    roots.append(prefix / "lib" / "site-packages" / "nvidia")

    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            bin_dir = child / "bin"
            key = os.path.normcase(str(bin_dir))
            if key not in seen and bin_dir.is_dir():
                seen.add(key)
                found.append(bin_dir)
    return found


def prepare_cuda_dlls() -> None:
    """Windows: adiciona site-packages/nvidia/*/bin ao caminho de DLLs
    (os.add_dll_directory + PATH) para o ctranslate2 achar cuBLAS/cuDNN instalados
    via pip. Idempotente; não faz nada fora do Windows ou se as pastas não existirem."""
    if sys.platform != "win32":
        return
    with _dll_lock:
        for bin_dir in _nvidia_bin_dirs():
            key = os.path.normcase(str(bin_dir))
            if key in _dll_dirs_done:
                continue
            _dll_dirs_done.add(key)
            try:
                _dll_handles.append(os.add_dll_directory(str(bin_dir)))
            except (OSError, AttributeError) as exc:
                logger.debug("add_dll_directory falhou para %s: %s", bin_dir, exc)
            path_dirs = os.environ.get("PATH", "").split(os.pathsep)
            if key not in map(os.path.normcase, path_dirs):
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            logger.debug("Biblioteca CUDA registrada: %s", bin_dir)


# --------------------------------------------------------------------------
# Testes reais com o FFmpeg
# --------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float) -> tuple[bool, str]:
    """Roda um comando; devolve (sucesso, stderr). Nunca levanta."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, f"{cmd[0]} não encontrado"
    except subprocess.TimeoutExpired:
        return False, f"tempo esgotado ({timeout:.0f} s)"
    except OSError as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stderr or "").strip()


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    # "Nothing was written into output file" é só consequência; a causa vem antes.
    useful = [ln for ln in lines if "Nothing was written" not in ln]
    chosen = useful or lines
    for ln in chosen:
        if any(k in ln for k in ("No capable", "Cannot", "not supported", "Failed", "failed")):
            return ln
    return chosen[-1] if chosen else "sem detalhes"


def _test_encoder(kind: str) -> tuple[bool, str]:
    codec = _ENCODER_CODECS[kind]
    cmd = [
        config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
        "-frames:v", "30", "-c:v", codec, "-f", "null", "-",
    ]
    return _run(cmd, _TEST_TIMEOUT_S)


def _test_decoder(kind: str) -> tuple[bool, str]:
    """Decode real: gera um h264 de 1 s em arquivo temporário e o decodifica com o hwaccel."""
    with tempfile.TemporaryDirectory(prefix="ac_hwtest_") as tmp:
        sample = Path(tmp) / "t.mp4"
        ok, err = _run([
            config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(sample),
        ], _TEST_TIMEOUT_S)
        if not ok:
            return False, err
        return _run([
            config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-hwaccel", kind,
            "-i", str(sample), "-f", "null", "-",
        ], _TEST_TIMEOUT_S)


def _gpu_names() -> tuple[str, ...]:
    names: list[str] = []
    if sys.platform == "win32":
        cmds = [["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"]]
    else:
        cmds = []
    cmds.append(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    for cmd in cmds:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_QUERY_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in str(proc.stdout or "").splitlines():
            line = line.strip()
            if line and line not in names:
                names.append(line)
        if names:
            break
    return tuple(names)


def _cuda_status() -> tuple[bool, str]:
    """(há CUDA com float16?, explicação). Não levanta."""
    prepare_cuda_dlls()
    try:
        import ctranslate2
    except ImportError:
        return False, "ctranslate2 não instalado"
    except Exception as exc:  # noqa: BLE001
        return False, f"ctranslate2 falhou ao importar ({exc})"
    try:
        count = int(ctranslate2.get_cuda_device_count())
        if count <= 0:
            return False, "nenhuma GPU CUDA visível ao ctranslate2"
        types = ctranslate2.get_supported_compute_types("cuda")
    except Exception as exc:  # noqa: BLE001
        return False, f"consulta CUDA falhou ({exc})"
    if "float16" not in types:
        return False, "a GPU CUDA não suporta float16"
    return True, f"{count} GPU(s) CUDA com float16"


# --------------------------------------------------------------------------
# Resolução do perfil
# --------------------------------------------------------------------------
def _resolve_video(notes: list[str]) -> tuple[str, str | None]:
    wanted = str(config.VIDEO_ENCODER).lower()
    if wanted != "auto":
        if wanted not in _VALID_ENCODERS:
            notes.append(f"VIDEO_ENCODER={wanted!r} inválido; usando CPU (libx264).")
            encoder = "cpu"
        else:
            encoder = wanted
            notes.append(f"Encoder de vídeo forçado no config: {encoder}.")
        forced = True
    else:
        encoder = "cpu"
        forced = False
        for kind in _AUTO_ENCODER_ORDER:
            ok, err = _test_encoder(kind)
            if ok:
                encoder = kind
                notes.append(f"Encode por GPU validado: {_ENCODER_CODECS[kind]}.")
                break
            notes.append(f"{_ENCODER_CODECS[kind]} indisponível ({_last_line(err)}).")
        if encoder == "cpu":
            notes.append("Nenhum encoder de GPU funcionou; usando CPU (libx264).")

    wanted_dec = str(config.HW_DECODE).lower()
    decode: str | None
    if wanted_dec == "none":
        decode = None
        notes.append("Decode por hardware desativado no config.")
    elif wanted_dec != "auto":
        if wanted_dec in _VALID_DECODERS:
            decode = wanted_dec
            notes.append(f"Decode forçado no config: {decode}.")
        else:
            decode = None
            notes.append(f"HW_DECODE={wanted_dec!r} inválido; sem decode por hardware.")
    else:
        candidate = _DEFAULT_DECODE.get(encoder)
        decode = None
        if candidate is None:
            notes.append("Sem decode por hardware (encode na CPU).")
        elif forced:
            decode = candidate
            notes.append(f"Decode {candidate} escolhido para o encoder {encoder}.")
        else:
            ok, err = _test_decoder(candidate)
            if ok:
                decode = candidate
                notes.append(f"Decode por hardware validado: {candidate}.")
            else:
                notes.append(f"Decode {candidate} indisponível ({_last_line(err)}); decode na CPU.")
    return encoder, decode


def _resolve_whisper(notes: list[str]) -> tuple[str, str, str]:
    wanted = str(config.WHISPER_DEVICE).lower()
    if wanted == "auto":
        ok, why = _cuda_status()
        device = "cuda" if ok else "cpu"
        notes.append(f"Whisper em {device}: {why}.")
    elif wanted in ("cuda", "cpu"):
        device = wanted
        notes.append(f"Dispositivo do Whisper forçado no config: {device}.")
    else:
        device = "cpu"
        notes.append(f"WHISPER_DEVICE={wanted!r} inválido; usando CPU.")

    compute = str(config.WHISPER_COMPUTE_TYPE)
    if compute.lower() == "auto":
        # NUNCA int8_float16: falha com CUBLAS_STATUS_NOT_SUPPORTED na série RTX 50.
        compute = "float16" if device == "cuda" else "int8"
    else:
        notes.append(f"Tipo de cálculo do Whisper forçado no config: {compute}.")

    model = str(config.WHISPER_MODEL)
    if model.lower() == "auto":
        if device == "cuda":
            vram = _cuda_vram_mib()
            # large-v3-turbo em float16 usa ~2 GB + ativações: só com folga de VRAM.
            model = _AUTO_MODEL_CUDA if vram >= _AUTO_MODEL_CUDA_MIN_VRAM_MIB else _AUTO_MODEL_CPU
            shown = f"{vram / 1024:.1f} GB" if vram else "desconhecida"
            notes.append(f"Modelo Whisper automático: {model} (VRAM {shown}).")
        else:
            model = _AUTO_MODEL_CPU
            notes.append(f"Modelo Whisper automático: {model}.")
    return device, compute, model


def _cuda_vram_mib() -> int:
    """Maior VRAM entre as GPUs NVIDIA (MiB) via nvidia-smi; 0 se não der para saber."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    values = []
    for line in (proc.stdout or "").splitlines():
        try:
            values.append(int(float(line.strip())))
        except ValueError:
            continue
    return max(values, default=0)


def detect(force: bool = False) -> HardwareProfile:
    """Perfil de hardware (cacheado; force=True refaz).

    Vídeo, quando config.VIDEO_ENCODER == "auto": tenta, nessa ordem, h264_nvenc,
    h264_qsv, h264_amf com um encode REAL de ~1 s (lavfi testsrc -> null, timeout
    curto); o primeiro que funcionar vence; senão "cpu". HW_DECODE "auto": cuda
    com nvenc, d3d11va com qsv/amf, None com cpu (também validado com um decode real).
    Whisper, quando WHISPER_DEVICE == "auto": prepare_cuda_dlls(); cuda se
    ctranslate2.get_cuda_device_count() > 0 e "float16" em
    get_supported_compute_types("cuda"); senão cpu. COMPUTE_TYPE "auto": float16
    em cuda (NUNCA int8_float16 — falha na série RTX 50), int8 em cpu.
    WHISPER_MODEL "auto": "large-v3-turbo" em cuda, "small" em cpu.
    Cada decisão vira uma frase em `notes`. Valores forçados no config são
    respeitados sem teste."""
    global _cached
    with _cache_lock:
        if _cached is not None and not force:
            return _cached
        notes: list[str] = []
        try:
            encoder, decode = _resolve_video(notes)
        except Exception as exc:  # noqa: BLE001 - nunca derruba o programa
            logger.warning("Detecção de vídeo falhou (%s); usando CPU", exc)
            encoder, decode = "cpu", None
            notes.append(f"Detecção de vídeo falhou ({exc}); usando CPU.")
        try:
            device, compute, model = _resolve_whisper(notes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Detecção do Whisper falhou (%s); usando CPU", exc)
            device, compute, model = "cpu", "int8", _AUTO_MODEL_CPU
            notes.append(f"Detecção do Whisper falhou ({exc}); usando CPU.")
        try:
            gpus = _gpu_names()
        except Exception:  # noqa: BLE001
            gpus = ()
        profile = HardwareProfile(
            video_encoder=encoder, hw_decode=decode, whisper_device=device,
            whisper_compute_type=compute, whisper_model=model,
            gpu_names=gpus, notes=tuple(notes),
        )
        _cached = profile
        logger.info(
            "Hardware: encode=%s decode=%s whisper=%s/%s/%s GPUs=%s",
            encoder, decode, device, compute, model, ", ".join(gpus) or "n/d",
        )
        return profile


def encoder_args(profile: HardwareProfile) -> list[str]:
    """Argumentos do FFmpeg para o encoder de vídeo do perfil (sem -i/-map):
    nvenc: -c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq <VIDEO_CRF+3> -b:v 0
    qsv:   -c:v h264_qsv -global_quality <VIDEO_CRF+3> -preset veryfast
    amf:   -c:v h264_amf -quality balanced -rc cqp -qp_i <..> -qp_p <..>
    cpu:   -c:v libx264 -crf <VIDEO_CRF> -preset <VIDEO_PRESET>
    (o +3 aproxima a mesma qualidade visual do CRF do x264; documente a escolha)."""
    quality = str(int(config.VIDEO_CRF) + _GPU_QUALITY_OFFSET)
    kind = profile.video_encoder
    if kind == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                "-cq", quality, "-b:v", "0"]
    if kind == "qsv":
        return ["-c:v", "h264_qsv", "-global_quality", quality, "-preset", "veryfast"]
    if kind == "amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                "-qp_i", quality, "-qp_p", quality]
    return ["-c:v", "libx264", "-crf", str(config.VIDEO_CRF), "-preset", str(config.VIDEO_PRESET)]


def decoder_args(profile: HardwareProfile) -> list[str]:
    """Argumentos ANTES do -i: ["-hwaccel", "cuda"] / ["-hwaccel", "d3d11va"] / [].
    Os quadros voltam para a memória do sistema (os filtros e a legenda rodam na CPU)."""
    if profile.hw_decode:
        return ["-hwaccel", profile.hw_decode]
    return []


# --------------------------------------------------------------------------
# Diagnóstico
# --------------------------------------------------------------------------
def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "não instalado"


def _ffmpeg_version() -> str:
    try:
        proc = subprocess.run(
            [config.FFMPEG_BIN, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_QUERY_TIMEOUT_S,
        )
        return (proc.stdout.splitlines() or ["?"])[0]
    except (OSError, subprocess.SubprocessError):
        return "não encontrado"


def _benchmark(profile: HardwareProfile) -> tuple[bool, float, str]:
    """Encode de 5 s 1080p30 (testsrc2) com o encoder do perfil. (ok, segundos, erro)."""
    cmd = [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={_BENCH_SECONDS}"]
    cmd += encoder_args(profile) + ["-f", "null", "-"]
    began = time.monotonic()
    ok, err = _run(cmd, _BENCH_TIMEOUT_S)
    return ok, time.monotonic() - began, err


def diagnose() -> str:
    """Relatório legível (pt-BR) para `main.py --diagnostico`: GPUs, perfil
    escolhido com as notes, versões (ffmpeg, ctranslate2, faster-whisper,
    streamlink), CUDA visível e um mini benchmark: encode de 5 s 1080p30 com o
    encoder escolhido vs CPU (tempos)."""
    profile = detect()
    lines: list[str] = ["=== Diagnóstico do Auto-Clipper ===", ""]
    lines.append("GPUs encontradas: " + (", ".join(profile.gpu_names) or "nenhuma detectada"))
    lines.append("")
    lines.append("Perfil escolhido:")
    lines.append(f"  encode de vídeo : {profile.video_encoder}")
    lines.append(f"  decode          : {profile.hw_decode or 'CPU'}")
    lines.append(f"  Whisper         : {profile.whisper_model} em {profile.whisper_device}/"
                 f"{profile.whisper_compute_type}")
    lines.append("")
    lines.append("Por quê:")
    lines.extend(f"  - {n}" for n in profile.notes)
    lines.append("")
    lines.append("Versões:")
    lines.append(f"  ffmpeg         : {_ffmpeg_version()}")
    lines.append(f"  ctranslate2    : {_version('ctranslate2')}")
    lines.append(f"  faster-whisper : {_version('faster-whisper')}")
    lines.append(f"  streamlink     : {_version('streamlink')}")
    lines.append("")
    ok, why = _cuda_status()
    lines.append(f"CUDA (ctranslate2): {'sim' if ok else 'não'} - {why}")
    dirs = _nvidia_bin_dirs()
    lines.append("Bibliotecas CUDA via pip: " + (", ".join(d.parent.name for d in dirs) or "nenhuma"))
    lines.append("")
    lines.append(f"Benchmark: encode de {_BENCH_SECONDS} s em 1080p30")
    cpu_profile = HardwareProfile("cpu", None, "cpu", "int8", "small")
    runs = [(profile.video_encoder, profile)]
    if profile.video_encoder != "cpu":
        runs.append(("cpu", cpu_profile))
    for label, prof in runs:
        done, secs, err = _benchmark(prof)
        if done:
            lines.append(f"  {label:<5}: {secs:.1f} s")
        else:
            lines.append(f"  {label:<5}: FALHOU ({_last_line(err)})")
    return "\n".join(lines)
