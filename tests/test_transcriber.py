from __future__ import annotations

import pytest

import transcriber
from transcriber import TranscriberError, format_srt


def test_basic_block():
    out = format_srt([{"start": 0.0, "end": 1.5, "text": " Olá "}])
    assert out == "1\n00:00:00,000 --> 00:00:01,500\nOlá\n"


def test_multiple_blocks_separated_by_blank_line():
    out = format_srt([
        {"start": 0, "end": 1, "text": "a"},
        {"start": 1, "end": 2, "text": "b"},
    ])
    assert out == "1\n00:00:00,000 --> 00:00:01,000\na\n\n2\n00:00:01,000 --> 00:00:02,000\nb\n"


def test_hours():
    out = format_srt([{"start": 3661.25, "end": 7322.5, "text": "x"}])
    assert "01:01:01,250 --> 02:02:02,500" in out


def test_rounding_carries():
    out = format_srt([{"start": 59.9996, "end": 60.0004, "text": "x"}])
    assert "00:01:00,000 --> 00:01:00,000" in out


def test_empty_text_skipped_and_renumbered():
    out = format_srt([
        {"start": 0, "end": 1, "text": "   "},
        {"start": 1, "end": 2, "text": "ok"},
        {"start": 2, "end": 3, "text": ""},
        {"start": 3, "end": 4, "text": "fim"},
    ])
    assert out == (
        "1\n00:00:01,000 --> 00:00:02,000\nok\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nfim\n"
    )


def test_empty_list():
    assert format_srt([]) == ""


def test_unicode():
    out = format_srt([{"start": 0, "end": 1, "text": "ação éção — 你好"}])
    assert "ação éção — 你好" in out
    assert out.endswith("\n")


def test_unsupported_engine(monkeypatch):
    monkeypatch.setattr(transcriber.config, "WHISPER_ENGINE", "faster")
    with pytest.raises(TranscriberError, match="engine não suportada"):
        transcriber.load_model()
