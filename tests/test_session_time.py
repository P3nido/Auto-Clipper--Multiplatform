"""Contrato do modelo de tempo (ADR-004)."""
from pathlib import Path

from session import RecordingSegment


def seg(**kw):
    return RecordingSegment(index=0, path=Path("live_00.ts"), wall_start=1000.0, wall_end=2000.0, **kw)


def test_offset_without_ads():
    assert seg().wall_to_offset(1100.0) == 100.0


def test_outside_segment_is_none():
    assert seg().wall_to_offset(999.0) is None
    assert seg().wall_to_offset(2001.0) is None


def test_ads_before_are_subtracted_and_inside_is_none():
    s = seg(ad_breaks=[(1050.0, 30.0)])  # anúncio de 1050 a 1080 foi removido do arquivo
    assert s.wall_to_offset(1040.0) == 40.0
    assert s.wall_to_offset(1060.0) is None
    assert s.wall_to_offset(1100.0) == 70.0
