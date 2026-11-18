"""build_video_filter é pura: testada sem ffmpeg."""
from __future__ import annotations

import pytest

from clipper import build_video_filter

SIZE = (1080, 1920)
STYLE = "FontSize=10,Alignment=2"


def _f(layout, srt="a.srt", fps=30):
    return build_video_filter(layout, srt, size=SIZE, fps=fps, style=STYLE)


def test_blur_structure():
    g = _f("vertical_blur", srt=None, fps=None)
    assert g.startswith("[0:v]format=yuv420p,split=2[bg][fg];")
    assert "scale=270:480" in g and "boxblur" in g and "scale=1080:1920" in g
    assert "scale=1080:-2" in g and "overlay=(W-w)/2:(H-h)/2" in g
    assert g.endswith("[v]") and "fps" not in g and "subtitles" not in g


def test_blur_fps_first_then_layout_then_subtitles():
    g = _f("vertical_blur")
    # fps antes do split: escala/desfoque/overlay processam só 30 quadros/s
    assert g.startswith("[0:v]format=yuv420p,fps=30,split=2")
    assert g.index("fps=30") < g.index("overlay") < g.index("subtitles=a.srt")
    assert "original_size=1080x1920" in g
    assert f"force_style='{STYLE}'" in g
    assert g.endswith("[v]")


def test_crop():
    g = _f("vertical_crop")
    assert g.startswith("[0:v]format=yuv420p,fps=30,crop=") and "scale=1080:1920" in g
    assert g.index("fps=30") < g.index("crop=") < g.index("subtitles=") and g.endswith("[v]")
    assert "[0:v]format=yuv420p,crop" in _f("vertical_crop", None, None) and _f("vertical_crop", None, None).endswith("setsar=1[v]")


def test_horizontal():
    assert build_video_filter("horizontal", None, size=SIZE, fps=None, style=STYLE) == "[0:v]format=yuv420p[v]"
    g = _f("horizontal")
    assert g.startswith("[0:v]format=yuv420p,fps=30,subtitles=a.srt:force_style=")
    assert "original_size" not in g and g.endswith("[v]")
    assert _f("horizontal", None, 30) == "[0:v]format=yuv420p,fps=30[v]"


def test_invalid_layout():
    with pytest.raises(ValueError):
        _f("diagonal")


def test_every_layout_normalizes_pixel_format_first():
    # Regressão: com decode por GPU (d3d11va/cuda) os quadros chegam em NV12 e o
    # fundo desfocado saía verde. format=yuv420p tem de ser o primeiro filtro.
    for layout in ("vertical_blur", "vertical_crop", "horizontal"):
        for fps in (30, None):
            g = build_video_filter(layout, "a.srt", size=SIZE, fps=fps, style=STYLE)
            assert g.startswith("[0:v]format=yuv420p")
