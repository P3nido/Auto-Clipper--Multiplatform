"""Entrada do Auto-Clipper.exe (janela, sem console; ADR-009)."""
from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    import gui

    sys.exit(gui.main())
