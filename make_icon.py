"""Gera gui_assets/icon.ico (fundo #101010 + 'play' recortado em #c8ff00)."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPolygonF

OUT = Path(__file__).resolve().parent / "gui_assets"
SIZES = (16, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#101010"))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.2, size * 0.2)
    p.setBrush(QColor("#c8ff00"))
    s = size
    p.drawPolygon(QPolygonF([QPointF(s * 0.34, s * 0.24), QPointF(s * 0.34, s * 0.76),
                             QPointF(s * 0.78, s * 0.50)]))
    p.setBrush(QColor("#101010"))  # corte diagonal: sensação de "recorte"
    p.drawPolygon(QPolygonF([QPointF(s * 0.34, s * 0.58), QPointF(s * 0.52, s * 0.50),
                             QPointF(s * 0.34, s * 0.50)]))
    p.end()
    return img


def write_ico(path: Path) -> None:
    """ICO com várias resoluções (entradas PNG), sem depender do Pillow."""
    import struct

    pngs = [(size, (OUT / f"icon_{size}.png").read_bytes()) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    path.write_bytes(header + entries + blobs)


def main() -> int:
    app = QGuiApplication(sys.argv)  # noqa: F841
    OUT.mkdir(exist_ok=True)
    for size in SIZES:
        render(size).save(str(OUT / f"icon_{size}.png"))
    write_ico(OUT / "icon.ico")
    return 0


if __name__ == "__main__":
    sys.exit(main())
