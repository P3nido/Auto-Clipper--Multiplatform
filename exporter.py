"""Envio dos clipes para o Google Drive pela pasta sincronizada (ADR-011).

Este módulo só COPIA arquivos para a pasta do Google Drive para desktop (ou para
qualquer outra pasta escolhida em Ajustes). O upload para a nuvem é feito pelo
próprio Drive para desktop, então não há login, token nem API.

Não depende de Qt: a GUI chama `export_clips` numa thread e repassa o progresso.
"""
from __future__ import annotations

import logging
import os
import shutil
import string
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_COPY_CHUNK = 1024 * 1024

__all__ = [
    "ExportError", "ExportResult", "detect_drive_folder", "resolve_export_dir",
    "destination_for", "is_already_exported", "export_clips",
]


class ExportError(Exception):
    """Falha ao copiar (destino inexistente/sem permissão, disco cheio...)."""


@dataclass(frozen=True)
class ExportResult:
    dest_dir: Path
    copied: tuple[Path, ...]     # caminhos FINAIS no destino
    skipped: tuple[Path, ...]    # origens puladas: já existiam no destino com o mesmo tamanho


def _drive_letters() -> list[Path]:
    """Raízes das unidades existentes (A:/ a Z:/). Vazio fora do Windows."""
    if sys.platform != "win32":
        return []
    roots: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        try:
            if root.exists():
                roots.append(root)
        except OSError:
            continue
    return roots


def _candidate_roots() -> list[Path]:
    """Candidatos a raiz do Drive, na ordem de preferência."""
    names = config.DRIVE_ROOT_NAMES
    home = Path.home()
    candidates: list[Path] = []
    for drive in _drive_letters():
        candidates.extend(drive / name for name in names)
    candidates.extend(home / "Google Drive" / name for name in names)
    candidates.append(home / "Google Drive")
    candidates.extend(home / name for name in names)
    return candidates


def detect_drive_folder() -> Path | None:
    """Raiz "Meu Drive"/"My Drive" do Google Drive para desktop, ou None.

    Ordem: unidades do Windows, depois home/"Google Drive", depois home/<nome>.
    """
    for candidate in _candidate_roots():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def resolve_export_dir(setting: str | None) -> Path | None:
    """Ajuste `export_dir` → pasta-base do envio, ou None se não houver destino."""
    text = (setting or "").strip()
    if not text or text == config.EXPORT_AUTO:
        root = detect_drive_folder()
        return None if root is None else root / config.EXPORT_SUBFOLDER
    return Path(text)


def destination_for(base_dir: Path, session_dir: Path | None, clips: Sequence[Path]) -> Path:
    """Pasta de destino de uma live: base_dir / <nome da pasta da sessão>."""
    if session_dir is not None and session_dir.name:
        return base_dir / session_dir.name
    if clips:
        name = Path(clips[0]).parent.parent.name
        if name:
            return base_dir / name
    return base_dir


def is_already_exported(clip: Path, dest_dir: Path) -> bool:
    """True se dest_dir/clip.name existe com o mesmo tamanho em bytes do clipe."""
    try:
        target = dest_dir / Path(clip).name
        return target.is_file() and target.stat().st_size == Path(clip).stat().st_size
    except OSError:
        return False


def _free_name(dest_dir: Path, clip: Path) -> Path:
    """Nome livre no destino: "<stem> (2)<ext>", "(3)"..."""
    n = 2
    while True:
        candidate = dest_dir / f"{clip.stem} ({n}){clip.suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _copy_atomic(src: Path, final: Path) -> None:
    part = final.with_name(final.name + ".part")
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            shutil.copyfileobj(fin, fout, _COPY_CHUNK)
        os.replace(part, final)
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            logger.warning("Não consegui apagar %s", part)
        raise


def export_clips(clips: Sequence[Path], dest_dir: Path, *,
                 on_progress: Callable[[int, int], None] | None = None,
                 stop_event: threading.Event | None = None) -> ExportResult:
    """Copia os clipes para dest_dir (criada se preciso, com pais)."""
    clips = [Path(c) for c in clips]
    total = len(clips)
    for clip in clips:
        if not clip.is_file():
            raise ExportError(f"Clipe não encontrado: {clip.name}")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Não foi possível criar a pasta {dest_dir}: {exc}") from exc

    copied: list[Path] = []
    skipped: list[Path] = []
    for index, clip in enumerate(clips, start=1):
        if stop_event is not None and stop_event.is_set():
            logger.info("Envio interrompido após %d de %d", index - 1, total)
            break
        try:
            if is_already_exported(clip, dest_dir):
                skipped.append(clip)
            else:
                final = dest_dir / clip.name
                if final.exists():
                    final = _free_name(dest_dir, clip)
                _copy_atomic(clip, final)
                copied.append(final)
                logger.info("Clipe copiado: %s", final)
        except OSError as exc:
            raise ExportError(f"Falha ao copiar {clip.name}: {exc}") from exc
        if on_progress is not None:
            on_progress(index, total)
    return ExportResult(dest_dir=dest_dir, copied=tuple(copied), skipped=tuple(skipped))
