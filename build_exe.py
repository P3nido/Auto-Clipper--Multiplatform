"""Gera o executável do Auto-Clipper (ADR-009).

Uso: python build_exe.py

1. limpa build/ e dist/;  2. roda o PyInstaller com auto_clipper.spec;
3. copia ffmpeg/ffprobe para dist/Auto-Clipper/ffmpeg/ e escreve o LEIA-ME.txt;
4. roda `auto-clipper-cli.exe --diagnostico` e falha se der erro.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["main", "find_tool"]

logger = logging.getLogger("build_exe")

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "auto_clipper.spec"
DIST = ROOT / "dist" / "Auto-Clipper"
TOOLS = ("ffmpeg", "ffprobe")
DIAGNOSTIC_TIMEOUT_S = 600

README_TEXT = r"""AUTO-CLIPPER
============

Como abrir
  Dê dois cliques em Auto-Clipper.exe. Cole o link da live (Twitch, YouTube ou Kick)
  e clique para iniciar. Não precisa instalar Python nem FFmpeg.
  (auto-clipper-cli.exe é a versão de linha de comando; use --diagnostico para ver
  a placa de vídeo e o encoder que serão usados.)

Onde ficam os clipes
  Em Vídeos\Auto-Clipper (pasta do seu usuário). Cada live ganha uma pasta com a
  gravação, o chat e os clipes finais. Os ajustes e o registro (app.log) ficam em
  %LOCALAPPDATA%\Auto-Clipper, então você pode trocar a pasta do programa por uma
  versão nova sem perder nada.

Primeira vez com legendas
  O modelo de transcrição (Whisper) é baixado uma única vez (cerca de 1,6 GB com
  placa NVIDIA). A interface avisa "Preparando as legendas". Precisa de internet.

Kick
  Para gravar na Kick é necessário ter o Google Chrome ou o Chromium instalado.

Licenças
  O FFmpeg incluído (pasta ffmpeg) é a build "full" do projeto Gyan, licenciada sob
  GPL v3: este pacote é para uso pessoal. Código-fonte e licença em
  https://www.gyan.dev/ffmpeg/builds/ e https://ffmpeg.org/legal.html.
  Streamlink (BSD-2), faster-whisper/CTranslate2 (MIT), PySide6/Qt (LGPL v3),
  NVIDIA cuBLAS/cuDNN (licença NVIDIA) e demais bibliotecas em _internal mantêm
  suas respectivas licenças.
"""


def find_tool(name: str) -> Path | None:
    """ffmpeg/ffprobe: PATH, link do winget e pasta de pacotes do winget (Gyan)."""
    exe = f"{name}.exe" if sys.platform == "win32" else name
    candidates: list[Path] = []
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        winget = Path(local) / "Microsoft" / "WinGet"
        candidates.append(winget / "Links" / exe)
        candidates.extend(sorted((winget / "Packages").glob(f"Gyan.FFmpeg*/**/bin/{exe}")))
    for cand in candidates:
        try:
            real = cand.resolve()
        except OSError:
            continue
        # O link do winget pode ser um atalho minúsculo; o binário real tem dezenas de MB.
        if real.is_file() and real.stat().st_size > 5 * 1024 * 1024:
            return real
    return None


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", **kwargs)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    started = time.monotonic()

    tools: dict[str, Path] = {}
    for name in TOOLS:
        path = find_tool(name)
        if path is None:
            logger.error("%s não encontrado (PATH/winget). Instale: winget install Gyan.FFmpeg", name)
            return 1
        tools[name] = path

    for folder in ("build", "dist"):
        shutil.rmtree(ROOT / folder, ignore_errors=True)

    logger.info("Rodando o PyInstaller (pode levar vários minutos)...")
    proc = _run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"], cwd=ROOT)
    if proc.returncode != 0:
        logger.error("PyInstaller falhou (código %d)", proc.returncode)
        return proc.returncode

    ffmpeg_dir = DIST / "ffmpeg"
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    for name, path in tools.items():
        shutil.copy2(path, ffmpeg_dir / path.name)
        logger.info("Copiado: %s", path)
    (DIST / "LEIA-ME.txt").write_text(README_TEXT, encoding="utf-8")

    cli = DIST / "auto-clipper-cli.exe"
    logger.info("Teste do pacote: %s --diagnostico", cli.name)
    # PATH sem winget/ffmpeg: garante que o diagnóstico ache o FFmpeg EMBUTIDO, não o do sistema.
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if "winget" not in p.lower() and "ffmpeg" not in p.lower()
    )
    result = _run([str(cli), "--diagnostico"], capture_output=True, env=env,
                  timeout=DIAGNOSTIC_TIMEOUT_S)
    sys.stdout.write(result.stdout or "")
    if result.returncode != 0:
        logger.error("--diagnostico falhou (código %d):\n%s", result.returncode, result.stderr)
        return 1

    size_gb = _dir_size(DIST) / 1024 ** 3
    logger.info("Pronto: %s (%.2f GB) em %.0f s", DIST, size_gb, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
