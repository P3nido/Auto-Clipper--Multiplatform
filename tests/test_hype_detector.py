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
