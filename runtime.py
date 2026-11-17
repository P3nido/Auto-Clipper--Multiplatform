"""Diferenças entre rodar do código-fonte e do executável empacotado (ADR-009).

Tudo que depende de "onde estou rodando" passa por aqui; os outros módulos não
testam sys.frozen diretamente.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "is_frozen",
    "app_dir",
    "data_dir",
    "default_sessions_dir",
    "bundled_tool",
    "streamlink_command",
    "subprocess_flags",
    "nvidia_dll_dirs",
]

APP_NAME = "Auto-Clipper"
CLI_EXE_NAME = "auto-clipper-cli.exe"
STREAMLINK_FLAG = "--run-streamlink"


_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_SESSIONS = "sessions"


def is_frozen() -> bool:
    """True quando rodando de dentro do executável do PyInstaller (sys.frozen)."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Pasta do .exe (empacotado) ou raiz do projeto (código-fonte)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _PROJECT_ROOT


def data_dir() -> Path:
    """Onde ficam settings.json e app.log: %LOCALAPPDATA%/Auto-Clipper (empacotado,
    criada se não existir) ou a raiz do projeto (código-fonte)."""
    if not is_frozen():
        return _PROJECT_ROOT
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / APP_NAME if base else Path.home() / ".auto-clipper"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def default_sessions_dir() -> str:
    """Pasta padrão das gravações: <Vídeos do usuário>/Auto-Clipper (empacotado) ou
    "sessions" (código-fonte, relativo ao projeto como hoje)."""
    if not is_frozen():
        return _DEFAULT_SESSIONS
    return str(Path.home() / "Videos" / APP_NAME)


def bundled_tool(name: str) -> str | None:
    """Caminho de app_dir()/ffmpeg/<name>.exe se existir (empacotado); senão None."""
    if not is_frozen():
        return None
    exe = name if name.lower().endswith(".exe") or sys.platform != "win32" else name + ".exe"
    candidate = app_dir() / "ffmpeg" / exe
    return str(candidate) if candidate.is_file() else None


def streamlink_command() -> list[str]:
    """Prefixo do comando do Streamlink:
    empacotado → [app_dir()/auto-clipper-cli.exe, "--run-streamlink"];
    código-fonte → [streamlink.exe ao lado de sys.executable ou do PATH]
    (mesma busca que recorder._find_streamlink faz hoje; RecorderError fica no recorder).

    Levanta FileNotFoundError se o executável não existir."""
    if is_frozen():
        cli = app_dir() / CLI_EXE_NAME
        if not cli.is_file():
            raise FileNotFoundError(f"{CLI_EXE_NAME} não encontrado em {app_dir()}")
        return [str(cli), STREAMLINK_FLAG]
    name = "streamlink.exe" if sys.platform == "win32" else "streamlink"
    local = Path(sys.executable).parent / name
    if local.is_file():
        return [str(local)]
    found = shutil.which("streamlink")
    if found:
        return [found]
    raise FileNotFoundError("streamlink não encontrado ao lado do Python nem no PATH")


def subprocess_flags() -> int:
    """creationflags para subprocess no Windows: CREATE_NO_WINDOW (nenhuma janela de
    console pisca quando a interface chama ffmpeg/ffprobe/nvidia-smi/powershell/streamlink).
    0 fora do Windows. Combine com | se o chamador já usa outras flags."""
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def nvidia_dll_dirs() -> list[Path]:
    """Pastas nvidia/*/bin onde estão cuBLAS/cuDNN: no venv (site-packages/nvidia) e,
    empacotado, dentro de sys._MEIPASS/nvidia."""
    import importlib.util

    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "nvidia")
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
