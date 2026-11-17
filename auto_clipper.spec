# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller (ADR-009): pasta dist/Auto-Clipper com dois .exe que compartilham _internal/.

Escolha: UMA Analysis com os dois launchers (em vez de duas Analysis + MERGE). Assim as
dependências são analisadas uma vez, e cada EXE recebe só o seu launcher (mais os runtime
hooks), filtrando a.scripts. Os dois EXE entram no mesmo COLLECT.

Uso: python build_exe.py   (ou: pyinstaller auto_clipper.spec --noconfirm)
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH)
ICON = str(ROOT / "gui_assets" / "icon.ico")
LAUNCHERS = {"launcher_gui", "launcher_cli"}

datas, binaries, hiddenimports = [], [], []
for pkg in (
    "streamlink", "streamlink_cli", "faster_whisper", "ctranslate2", "curl_cffi", "av",
    "onnxruntime", "tokenizers", "huggingface_hub",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Plugins do streamlink são carregados dinamicamente; trio* para o webbrowser (Kick).
hiddenimports += collect_submodules("streamlink.plugins")
hiddenimports += collect_submodules("streamlink.webbrowser")
hiddenimports += ["trio", "trio_websocket", "wsproto", "outcome", "sniffio", "exceptiongroup"]
# Módulos do projeto importados dentro de funções.
hiddenimports += ["config", "runtime", "main", "gui", "engine", "hardware", "transcriber",
                  "clipper", "recorder", "chat_listener", "hype_detector", "session"]

# DLLs CUDA (cuBLAS/cuDNN) do venv -> _internal/nvidia/<pacote>/bin (runtime.nvidia_dll_dirs).
import sysconfig
nvidia_root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
if nvidia_root.is_dir():
    for dll in nvidia_root.glob("*/bin/*.dll"):
        binaries.append((str(dll), f"nvidia/{dll.parent.parent.name}/bin"))

excludes = [
    "torch", "torchgen", "functorch", "torchvision", "torchaudio", "whisper", "triton",
    "numba", "llvmlite", "tiktoken", "tkinter", "_tkinter", "matplotlib", "IPython", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
]

a = Analysis(
    [str(ROOT / "launcher_gui.py"), str(ROOT / "launcher_cli.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)


def scripts_for(launcher: str):
    """Runtime hooks + apenas o launcher pedido."""
    return [s for s in a.scripts if s[0] not in LAUNCHERS or s[0] == launcher]


gui_exe = EXE(
    pyz, scripts_for("launcher_gui"), [],
    exclude_binaries=True, name="Auto-Clipper", console=False, icon=ICON, upx=False,
)
cli_exe = EXE(
    pyz, scripts_for("launcher_cli"), [],
    exclude_binaries=True, name="auto-clipper-cli", console=True, icon=ICON, upx=False,
)
coll = COLLECT(
    gui_exe, cli_exe, a.binaries, a.datas,
    strip=False, upx=False, name="Auto-Clipper",
)
