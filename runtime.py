"""Diferenças entre rodar do código-fonte e do executável empacotado (ADR-009).

Tudo que depende de "onde estou rodando" passa por aqui; os outros módulos não
testam sys.frozen diretamente.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
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
    "create_kill_on_close_job",
    "assign_to_kill_on_close_job",
    "close_job",
    "kill_children_on_exit",
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


# --------------------------------------------------------------------------
# Windows Job Object: filhos morrem junto com o app (mesmo em crash)
# --------------------------------------------------------------------------
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_app_job: int | None = None
_app_job_lock = threading.Lock()


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetCurrentProcessId.restype = wintypes.DWORD
    k.GetCurrentProcessId.argtypes = []
    return k


def create_kill_on_close_job() -> int | None:
    """Cria um Job Object com KILL_ON_JOB_CLOSE e devolve o handle (int), ou None se não
    for Windows / a API falhar (nunca levanta)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k = _kernel32()
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(job)
            return None
        return int(job)
    except Exception:  # noqa: BLE001 - fallback seguro: sem Job Object
        return None


def _assign_pid(job: int, pid: int) -> bool:
    k = _kernel32()
    handle = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(k.AssignProcessToJobObject(job, handle))
    finally:
        k.CloseHandle(handle)


def assign_to_kill_on_close_job(proc: object, job: int | None = None) -> bool:
    """Associa o subprocesso (Popen) ao `job` informado ou, sem ele, ao job do app (criado
    por kill_children_on_exit; sem ele não faz nada, pois o filho já herda o job do pai).
    False se não aplicável/falhou; nunca levanta."""
    if sys.platform != "win32":
        return False
    try:
        target = job if job is not None else _app_job
        if target is None:
            return False
        return _assign_pid(int(target), int(proc.pid))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def close_job(job: int | None) -> None:
    """Fecha o handle do job; com KILL_ON_JOB_CLOSE, todos os processos nele morrem."""
    if job is None or sys.platform != "win32":
        return
    try:
        _kernel32().CloseHandle(int(job))
    except Exception:  # noqa: BLE001
        pass


def kill_children_on_exit() -> bool:
    """Coloca o PRÓPRIO processo num Job Object KILL_ON_JOB_CLOSE: os filhos herdam o job e
    morrem quando o app sai ou cai (o Windows fecha o handle). Idempotente; False fora do
    Windows ou se a API falhar (aí vale só o registro de processos do recorder)."""
    global _app_job
    if sys.platform != "win32":
        return False
    with _app_job_lock:
        if _app_job is not None:
            return True
        job = create_kill_on_close_job()
        if job is None:
            return False
        try:
            if not _assign_pid(job, int(_kernel32().GetCurrentProcessId())):
                close_job(job)
                return False
        except Exception:  # noqa: BLE001
            close_job(job)
            return False
        _app_job = job
        return True
