"""Testes do hardware.py: perfis, forçados no config e detecção com subprocess mockado."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types

import pytest

import config
import hardware
from hardware import HardwareProfile


def _profile(encoder="cpu", decode=None, device="cpu", compute="int8", model="small") -> HardwareProfile:
    return HardwareProfile(encoder, decode, device, compute, model)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Cache limpo, config em auto e sem consultas reais a GPU/PowerShell."""
    monkeypatch.setattr(hardware, "_cached", None)
    for key, value in (("VIDEO_ENCODER", "auto"), ("HW_DECODE", "auto"), ("WHISPER_DEVICE", "auto"),
                       ("WHISPER_MODEL", "auto"), ("WHISPER_COMPUTE_TYPE", "auto"),
                       ("VIDEO_CRF", 18), ("VIDEO_PRESET", "veryfast")):
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(hardware, "_gpu_names", lambda: ("GPU Falsa",))
    monkeypatch.setattr(hardware, "_cuda_status", lambda: (False, "sem CUDA (teste)"))
    yield
    hardware._cached = None


def _fake_tests(monkeypatch, working_encoders=(), working_decoders=()):
    monkeypatch.setattr(
        hardware, "_test_encoder",
        lambda kind: (kind in working_encoders, "" if kind in working_encoders else "falhou"),
    )
    monkeypatch.setattr(
        hardware, "_test_decoder",
        lambda kind: (kind in working_decoders, "" if kind in working_decoders else "falhou"),
    )


# --- argumentos ----------------------------------------------------------
def test_encoder_args_nvenc():
    args = hardware.encoder_args(_profile("nvenc", "cuda"))
    assert args[:2] == ["-c:v", "h264_nvenc"]
    assert args[args.index("-cq") + 1] == "21"
    assert args[args.index("-b:v") + 1] == "0"
    assert "p5" in args and "hq" in args and "vbr" in args


def test_encoder_args_qsv():
    args = hardware.encoder_args(_profile("qsv", "d3d11va"))
    assert args == ["-c:v", "h264_qsv", "-global_quality", "21", "-preset", "veryfast"]


def test_encoder_args_amf():
    args = hardware.encoder_args(_profile("amf", "d3d11va"))
    assert args[:2] == ["-c:v", "h264_amf"]
    assert args[args.index("-qp_i") + 1] == "21" and args[args.index("-qp_p") + 1] == "21"
    assert "cqp" in args


def test_encoder_args_cpu_uses_config(monkeypatch):
    monkeypatch.setattr(config, "VIDEO_CRF", 20)
    monkeypatch.setattr(config, "VIDEO_PRESET", "fast")
    assert hardware.encoder_args(_profile()) == ["-c:v", "libx264", "-crf", "20", "-preset", "fast"]
    nvenc = hardware.encoder_args(_profile("nvenc"))
    assert nvenc[nvenc.index("-cq") + 1] == "23"


@pytest.mark.parametrize("decode,expected", [
    ("cuda", ["-hwaccel", "cuda"]), ("d3d11va", ["-hwaccel", "d3d11va"]), (None, []),
])
def test_decoder_args(decode, expected):
    assert hardware.decoder_args(_profile("cpu", decode)) == expected


# --- detect --------------------------------------------------------------
def test_detect_qsv_when_nvenc_fails(monkeypatch):
    _fake_tests(monkeypatch, working_encoders=("qsv",), working_decoders=("d3d11va",))
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("qsv", "d3d11va")
    assert p.gpu_names == ("GPU Falsa",)
    assert any("nvenc" in n for n in p.notes)


def test_detect_nvenc_first(monkeypatch):
    _fake_tests(monkeypatch, working_encoders=("nvenc", "qsv"), working_decoders=("cuda", "d3d11va"))
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("nvenc", "cuda")


def test_detect_everything_fails_is_cpu(monkeypatch):
    _fake_tests(monkeypatch)
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("cpu", None)
    assert (p.whisper_device, p.whisper_compute_type, p.whisper_model) == ("cpu", "int8", "small")


def test_detect_decoder_fails_keeps_encoder(monkeypatch):
    _fake_tests(monkeypatch, working_encoders=("qsv",))
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("qsv", None)


def test_detect_with_subprocess_mocked(monkeypatch):
    """Mocka o subprocess.run: h264_nvenc/amf falham, h264_qsv e o decode d3d11va funcionam."""
    def fake_run(cmd, **kwargs):
        text = " ".join(map(str, cmd))
        bad = "h264_nvenc" in text or "h264_amf" in text or "-hwaccel cuda" in text
        return subprocess.CompletedProcess(
            cmd, 1 if bad else 0, stdout="", stderr="No capable devices found" if bad else ""
        )

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("qsv", "d3d11va")


def test_detect_ffmpeg_missing_is_cpu(monkeypatch):
    monkeypatch.setattr(config, "FFMPEG_BIN", "ffmpeg-inexistente-xyz")
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("cpu", None)


def test_detect_is_cached(monkeypatch):
    calls: list[str] = []

    def fake(kind):
        calls.append(kind)
        return False, "x"

    monkeypatch.setattr(hardware, "_test_encoder", fake)
    first = hardware.detect()
    n = len(calls)
    assert hardware.detect() is first and len(calls) == n
    hardware.detect(force=True)
    assert len(calls) > n


def test_forced_values_respected_without_testing(monkeypatch):
    def boom(kind):
        raise AssertionError("não deveria testar valores forçados")

    monkeypatch.setattr(hardware, "_test_encoder", boom)
    monkeypatch.setattr(hardware, "_test_decoder", boom)
    monkeypatch.setattr(config, "VIDEO_ENCODER", "nvenc")
    monkeypatch.setattr(config, "HW_DECODE", "none")
    monkeypatch.setattr(config, "WHISPER_DEVICE", "cuda")
    monkeypatch.setattr(config, "WHISPER_MODEL", "medium")
    monkeypatch.setattr(config, "WHISPER_COMPUTE_TYPE", "float32")
    p = hardware.detect()
    assert (p.video_encoder, p.hw_decode) == ("nvenc", None)
    assert (p.whisper_device, p.whisper_compute_type, p.whisper_model) == ("cuda", "float32", "medium")


def test_forced_encoder_derives_decode(monkeypatch):
    monkeypatch.setattr(config, "VIDEO_ENCODER", "nvenc")
    assert hardware.detect().hw_decode == "cuda"
    monkeypatch.setattr(config, "VIDEO_ENCODER", "cpu")
    assert hardware.detect(force=True).hw_decode is None


def test_invalid_encoder_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(config, "VIDEO_ENCODER", "xyz")
    assert hardware.detect().video_encoder == "cpu"


def test_whisper_auto_cuda(monkeypatch):
    _fake_tests(monkeypatch)
    monkeypatch.setattr(hardware, "_cuda_status", lambda: (True, "1 GPU(s) CUDA com float16"))
    monkeypatch.setattr(hardware, "_cuda_vram_mib", lambda: 8151)  # RTX 5060
    p = hardware.detect()
    assert (p.whisper_device, p.whisper_compute_type, p.whisper_model) == ("cuda", "float16", "large-v3-turbo")


def test_compute_auto_never_int8_float16(monkeypatch):
    _fake_tests(monkeypatch)
    for cuda in (True, False):
        monkeypatch.setattr(hardware, "_cuda_status", lambda c=cuda: (c, ""))
        p = hardware.detect(force=True)
        assert p.whisper_compute_type in ("float16", "int8")
        assert "int8_float16" not in p.whisper_compute_type


def test_whisper_model_auto_vs_explicit(monkeypatch):
    _fake_tests(monkeypatch)
    assert hardware.detect().whisper_model == "small"
    monkeypatch.setattr(config, "WHISPER_MODEL", "tiny")
    assert hardware.detect(force=True).whisper_model == "tiny"


# --- DLLs CUDA -----------------------------------------------------------
def test_prepare_cuda_dlls_without_dirs_is_noop(monkeypatch):
    monkeypatch.setattr(hardware, "_nvidia_bin_dirs", lambda: [])
    before = os.environ["PATH"]
    hardware.prepare_cuda_dlls()
    hardware.prepare_cuda_dlls()
    assert os.environ["PATH"] == before


@pytest.mark.skipif(sys.platform != "win32", reason="add_dll_directory é do Windows")
def test_prepare_cuda_dlls_idempotent(monkeypatch, tmp_path):
    bin_dir = tmp_path / "cublas" / "bin"
    bin_dir.mkdir(parents=True)
    added: list[str] = []
    monkeypatch.setattr(hardware, "_nvidia_bin_dirs", lambda: [bin_dir])
    monkeypatch.setattr(
        hardware.os, "add_dll_directory",
        lambda p: added.append(p) or types.SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(hardware, "_dll_dirs_done", set())
    monkeypatch.setattr(hardware, "_dll_handles", [])
    monkeypatch.setenv("PATH", os.environ["PATH"])
    hardware.prepare_cuda_dlls()
    hardware.prepare_cuda_dlls()
    assert added == [str(bin_dir)]
    assert os.environ["PATH"].split(os.pathsep)[0] == str(bin_dir)
    assert os.environ["PATH"].split(os.pathsep).count(str(bin_dir)) == 1
    assert len(hardware._dll_handles) == 1


def test_nvidia_bin_dirs_finds_pip_layout(monkeypatch, tmp_path):
    site = tmp_path / "Lib" / "site-packages" / "nvidia"
    (site / "cublas" / "bin").mkdir(parents=True)
    (site / "cudnn" / "bin").mkdir(parents=True)
    (site / "nvtx" / "include").mkdir(parents=True)
    monkeypatch.setattr(hardware.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(hardware.importlib.util, "find_spec", lambda name: None)
    names = sorted(d.parent.name for d in hardware._nvidia_bin_dirs())
    assert names == ["cublas", "cudnn"]


# --- diagnóstico e integração real ---------------------------------------
def test_diagnose_report(monkeypatch):
    _fake_tests(monkeypatch)
    monkeypatch.setattr(hardware, "_benchmark", lambda prof: (True, 1.5, ""))
    text = hardware.diagnose()
    assert "GPU Falsa" in text and "Benchmark" in text and "cpu" in text


@pytest.mark.skipif(shutil.which(config.FFMPEG_BIN) is None, reason="ffmpeg indisponível")
def test_detect_real_returns_valid_profile():
    p = hardware.detect(force=True)  # ffmpeg real (GPU e CUDA continuam mockados pela fixture)
    assert p.video_encoder in ("nvenc", "qsv", "amf", "cpu")
    assert p.hw_decode in ("cuda", "d3d11va", "qsv", None)
    assert hardware.encoder_args(p)[0] == "-c:v"


def test_auto_model_depends_on_vram(monkeypatch):
    import config
    import hardware

    monkeypatch.setattr(config, "WHISPER_DEVICE", "cuda")
    monkeypatch.setattr(config, "WHISPER_COMPUTE_TYPE", "auto")
    monkeypatch.setattr(config, "WHISPER_MODEL", "auto")
    for vram, expected in [(8151, "large-v3-turbo"), (2048, "small"), (0, "small")]:
        monkeypatch.setattr(hardware, "_cuda_vram_mib", lambda v=vram: v)
        device, compute, model = hardware._resolve_whisper([])
        assert (device, compute, model) == ("cuda", "float16", expected)
