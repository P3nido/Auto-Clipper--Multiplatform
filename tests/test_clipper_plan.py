"""Contrato de clipper.plan_clips (lógica pura)."""
from clipper import ClipSpec, plan_clips
from hype_detector import Spike

KW = dict(pre_s=15, post_s=15, max_clips=30)


def spike(offset, count=20, seg=0):
    return Spike(segment_index=seg, video_offset_s=offset, count=count, baseline=4.0)


def test_window_around_peak():
    assert plan_clips([spike(100)], {0: 600}, **KW) == [ClipSpec(0, 85, 30, 20)]


def test_window_clamped_to_video_bounds():
    clips = plan_clips([spike(5), spike(595)], {0: 600}, **KW)
    assert clips[0].start_s == 0 and clips[0].duration_s == 20
    assert clips[1].start_s == 580 and clips[1].duration_s == 20


def test_overlapping_windows_are_merged():
    clips = plan_clips([spike(100, 10), spike(120, 30)], {0: 600}, **KW)
    assert clips == [ClipSpec(0, 85, 50, 30)]


def test_no_merge_across_segments():
    assert len(plan_clips([spike(100, seg=0), spike(100, seg=1)], {0: 600, 1: 600}, **KW)) == 2


def test_max_clips_keeps_strongest_in_chronological_order():
    spikes = [spike(100, 10), spike(200, 50), spike(300, 30)]
    clips = plan_clips(spikes, {0: 600}, pre_s=15, post_s=15, max_clips=2)
    assert [c.start_s for c in clips] == [185, 285]


def test_empty_and_unknown_segment_ignored():
    assert plan_clips([], {0: 600}, **KW) == []
    assert plan_clips([spike(100, seg=7)], {0: 600}, **KW) == []


def test_short_window_discarded():
    assert plan_clips([spike(599)], {0: 600}, pre_s=1, post_s=1, max_clips=5) == []


def test_max_clips_tie_prefers_earliest():
    clips = plan_clips([spike(100, 20), spike(300, 20)], {0: 600}, pre_s=15, post_s=15, max_clips=1)
    assert [c.start_s for c in clips] == [85]
