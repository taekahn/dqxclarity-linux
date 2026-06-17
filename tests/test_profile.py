"""Tests for the run --profile timing profiler and its serve() wiring.

Durations are injected directly via Profiler.record (no real sleeps) so the aggregation, slow-event
threshold, summary ordering, and cadence detection are deterministic.
"""

from __future__ import annotations

import threading

from dqxclarity.runtime.dispatch import serve
from dqxclarity.runtime.profile import SLOW_S, Profiler


def test_record_aggregates_count_total_max():
    p = Profiler()
    p.record("serve", "dialogue", 0.01)
    p.record("serve", "dialogue", 0.03)
    n, tot, mx = p.agg["serve:dialogue"]
    assert n == 2
    assert abs(tot - 0.04) < 1e-9
    assert abs(mx - 0.03) < 1e-9


def test_slow_threshold_keeps_timeline_and_fires_callback():
    fired = []
    p = Profiler(on_slow=lambda ts, kind, label, ms, detail: fired.append((kind, label, round(ms))))
    p.record("serve", "x", SLOW_S - 0.001)        # below threshold -> aggregated but NOT slow
    p.record("namescan", "warm", 0.05, "regions=3 hits=2")  # >= threshold -> slow
    assert len(p.slow) == 1                        # only the slow one kept in the timeline
    _ts, kind, label, dur, detail = p.slow[0]
    assert (kind, label) == ("namescan", "warm")
    assert detail == "regions=3 hits=2"
    assert fired == [("namescan", "warm", 50)]     # callback got ms, rounded


def test_summary_rows_sorted_by_total_descending():
    p = Profiler()
    p.record("serve", "small", 0.02)
    p.record("namescan", "full", 0.50)             # biggest total
    p.record("serve", "small", 0.01)
    rows = p.summary_rows()
    assert rows[0][0] == "namescan:full"           # sorted by total ms desc
    key, n, tot_ms, mean_ms, max_ms = rows[0]
    assert n == 1 and abs(tot_ms - 500.0) < 1e-6 and abs(max_ms - 500.0) < 1e-6


def test_cadence_hint_reports_median_gap_and_dominant_label():
    p = Profiler()
    # Inject a regular ~3s beat of warm scans plus one odd serve event.
    p.slow = [
        (0.0, "namescan", "warm", 1.9, ""),
        (3.0, "namescan", "warm", 1.9, ""),
        (6.0, "namescan", "warm", 1.9, ""),
        (6.2, "serve", "dialogue", 0.2, ""),
    ]
    hint = p.cadence_hint()
    assert hint is not None
    assert "namescan:warm" in hint and "3x" in hint
    assert "3.0s" in hint                          # median gap of [3.0, 3.0, 0.2] -> 3.0


def test_cadence_hint_none_when_too_few_events():
    p = Profiler()
    p.slow = [(0.0, "serve", "x", 0.1, "")]
    assert p.cadence_hint() is None


class _FakeHook:
    """serve_once returns a value once (a served field) then sets stop and returns None."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.calls = 0

    def serve_once(self, mem, fn):
        self.calls += 1
        if self.calls == 1:
            return "ja-line"
        self.stop.set()
        return None


def test_serve_records_a_profiler_event_per_real_serve():
    stop = threading.Event()
    p = Profiler()
    hook = _FakeHook(stop)
    served = serve(None, [("dialogue", hook, None)], stop=stop, profiler=p)
    assert served == 1
    assert "serve:dialogue" in p.agg          # the real serve was timed (ja was not None)
    assert p.agg["serve:dialogue"][0] == 1


def test_serve_without_profiler_is_a_noop_path():
    # Same fake hook, no profiler -> still serves, just no timing (regression guard for the branch).
    stop = threading.Event()
    hook = _FakeHook(stop)
    assert serve(None, [("dialogue", hook, None)], stop=stop) == 1
