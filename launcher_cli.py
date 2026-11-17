"""Entrada do auto-clipper-cli.exe (ADR-009).

Roda a CLI (main.main) ou, com --run-streamlink, o streamlink_cli embutido: é assim
que o recorder grava empacotado, sem precisar de um streamlink.exe separado.
"""
from __future__ import annotations

import multiprocessing
import sys

import runtime


def run() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == runtime.STREAMLINK_FLAG:
        sys.argv = ["streamlink", *sys.argv[2:]]
        from streamlink_cli.main import main as streamlink_main

        code = streamlink_main()
        return int(code or 0)
    import main

    return main.main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
