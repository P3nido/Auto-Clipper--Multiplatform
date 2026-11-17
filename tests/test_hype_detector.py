"""Contrato da etapa 3 (lógica pura). Esses testes definem o comportamento esperado."""
from hype_detector import bucketize, detect_spike_buckets

PARAMS = dict(bucket_s=10, multiplier=3.0, baseline_window=30, warmup=6, min_messages=5, cooldown_s=30)


def test_bucketize_counts_and_empty_buckets():
    ts = [100.0, 101.0, 109.9, 125.0]  # start=100 -> blocos [100,110) [110,120) [120,130)
    assert bucketize(ts, start=100.0, end=130.0, bucket_s=10) == [3, 0, 1]


def test_bucketize_ignores_out_of_range():
    assert bucketize([50.0, 100.0, 200.0], start=100.0, end=120.0, bucket_s=10) == [1, 0]


def test_flat_chat_has_no_spikes():
    assert detect_spike_buckets([4] * 60, **PARAMS) == []


def test_clear_spike_is_detected_with_baseline():
    counts = [4] * 20 + [20] + [4] * 10
    assert detect_spike_buckets(counts, **PARAMS) == [(20, 4.0)]


def test_warmup_ignores_early_spike():
    counts = [1, 1, 30] + [2] * 20
    assert all(i >= 6 for i, _ in detect_spike_buckets(counts, **PARAMS))


def test_min_messages_floor_blocks_tiny_spikes():
    counts = [1] * 20 + [4] + [1] * 5  # 4 >= 3x1, mas abaixo do piso de 5
    assert detect_spike_buckets(counts, **PARAMS) == []


def test_cooldown_keeps_strongest_bucket_of_a_burst():
    counts = [4] * 20 + [15, 25, 18] + [4] * 10  # rajada de 3 blocos (< 30 s)
    assert [i for i, _ in detect_spike_buckets(counts, **PARAMS)] == [21]


def test_spikes_separated_by_more_than_cooldown_are_both_kept():
    counts = [4] * 20 + [20] + [4] * 5 + [20] + [4] * 5
    assert [i for i, _ in detect_spike_buckets(counts, **PARAMS)] == [20, 26]


def test_bucketize_empty_inputs():
    assert bucketize([], start=0.0, end=25.0, bucket_s=10) == [0, 0, 0]
    assert bucketize([1.0], start=5.0, end=5.0, bucket_s=10) == []


def test_zero_baseline_uses_only_min_messages_floor():
    counts = [0] * 10 + [5] + [0] * 5
    assert detect_spike_buckets(counts, **PARAMS) == [(10, 0.0)]


def test_cooldown_tie_keeps_first_and_long_burst_is_single_peak():
    assert [i for i, _ in detect_spike_buckets([4] * 20 + [20, 20] + [4] * 10, **PARAMS)] == [20]
    assert len(detect_spike_buckets([4] * 20 + [30] * 8 + [4] * 5, **PARAMS)) == 1


def test_empty_counts():
    assert detect_spike_buckets([], **PARAMS) == []


# --- find_spikes (integração com Session) ---------------------------------
import json  # noqa: E402

import pytest  # noqa: E402

import config  # noqa: E402
from hype_detector import find_spikes  # noqa: E402
from session import Session  # noqa: E402

T0 = 1_000_000.0
URL = "https://www.twitch.tv/canal"


def _write_chat(session: Session, stamps: list[float], extra_lines: tuple[str, ...] = ()) -> None:
    with open(session.chat_path, "w", encoding="utf-8") as fh:
        for ts in stamps:
            fh.write(json.dumps({"platform": "twitch", "author": "a", "text": "x", "wall_ts": ts}) + "\n")
        for line in extra_lines:
            fh.write(line + "\n")


def _flat_with_burst(start: float, flat_buckets: int, burst_bucket: int, burst: int = 30) -> list[float]:
    """2 msgs por bloco de 10 s e `burst` msgs no bloco burst_bucket."""
    out: list[float] = []
    for b in range(flat_buckets):
        out += [start + b * 10 + 1, start + b * 10 + 6]
    out += [start + burst_bucket * 10 + 1 + i * 0.1 for i in range(burst)]
    return out


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHAT_DELAY_OFFSET_S", 0.0)
    return Session.create(URL, tmp_path)


def test_spike_at_right_offset(session):
    seg = session.new_segment()
    seg.wall_start, seg.wall_end = T0, T0 + 300
    _write_chat(session, _flat_with_burst(T0, 30, 20))
    spikes = find_spikes(session)
    assert len(spikes) == 1
    assert spikes[0].segment_index == 0
    assert spikes[0].video_offset_s == pytest.approx(205.0)
    assert spikes[0].count >= 30
    saved = json.loads(session.spikes_path.read_text(encoding="utf-8"))
    assert saved[0]["video_offset_s"] == pytest.approx(205.0)
    assert saved[0]["wall_ts"] == pytest.approx(T0 + 205.0)


def test_chat_delay_offset_shifts_spike(session, monkeypatch):
    seg = session.new_segment()
    seg.wall_start, seg.wall_end = T0, T0 + 300
    # chat reage 20 s depois: mensagens em T0+200 viram T0+180 após o desconto
    monkeypatch.setattr(config, "CHAT_DELAY_OFFSET_S", 20.0)
    _write_chat(session, _flat_with_burst(T0 + 20, 30, 20))
    spikes = find_spikes(session)
    assert spikes[0].video_offset_s == pytest.approx(205.0)


def test_spike_inside_ad_is_discarded(session):
    seg = session.new_segment()
    seg.wall_start, seg.wall_end = T0, T0 + 300
    seg.ad_breaks = [(T0 + 195, 30.0)]
    _write_chat(session, _flat_with_burst(T0, 30, 20))
    assert find_spikes(session) == []


def test_two_segments(session):
    s0 = session.new_segment()
    s0.wall_start, s0.wall_end = T0, T0 + 300
    s1 = session.new_segment()
    s1.wall_start, s1.wall_end = T0 + 1000, T0 + 1300
    _write_chat(session, _flat_with_burst(T0, 30, 20) + _flat_with_burst(T0 + 1000, 30, 15))
    spikes = find_spikes(session)
    assert [(s.segment_index, round(s.video_offset_s, 1)) for s in spikes] == [(0, 205.0), (1, 155.0)]


def test_segment_without_wall_start_ignored(session):
    session.new_segment()  # wall_start None
    _write_chat(session, _flat_with_burst(T0, 30, 20))
    assert find_spikes(session) == []


def test_wall_end_none_uses_last_message(session):
    seg = session.new_segment()
    seg.wall_start = T0
    _write_chat(session, _flat_with_burst(T0, 30, 20))
    spikes = find_spikes(session)
    assert len(spikes) == 1
    assert spikes[0].video_offset_s == pytest.approx(205.0)


def test_missing_chat_and_invalid_lines(session):
    seg = session.new_segment()
    seg.wall_start, seg.wall_end = T0, T0 + 300
    assert find_spikes(session) == []
    _write_chat(session, _flat_with_burst(T0, 30, 20), extra_lines=("lixo", '{"text": "sem ts"}'))
    assert len(find_spikes(session)) == 1
