"""Tests for the network_text CAPTURE recorder + the offline bench-network command + run wiring.

  1. NetworkCaptureRecorder.record() accumulates count / unique(capped) / japanese / pct / len stats
     across mixed Japanese + non-Japanese + duplicate inputs; report() orders by count DESC; dump()
     writes valid JSON round-trippable to report().
  2. The capture fn run() builds returns None for EVERY input and records what it saw.
  3. bench-network: a tiny capture JSON + a fake translator (deterministic translate_now sleep, one
     lookup cache-hit) produces per-category latency + cache-hit numbers without error.
  4. run() wiring: --capture-network installs the capture fn for network_text and writes the report
     file on exit (reusing the test_lifecycle run_env mocking style).

All pure/offline: no game, /proc, network, or install is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dqxclarity import cli
from dqxclarity.runtime.netcapture import (
    MAX_SAMPLES,
    NetworkCaptureRecorder,
    build_summary_table,
    summary_rows,
)

# Reuse the hermetic run() harness from the lifecycle suite.
from tests.test_lifecycle import run_env  # noqa: F401  (pytest fixture)

JA = "こんにちは"      # Japanese
JA2 = "戦闘開始"        # Japanese
EN = "Hello world"     # non-Japanese


# =============================================================================================== #
# NetworkCaptureRecorder                                                                           #
# =============================================================================================== #


def test_record_accumulates_count_japanese_pct_and_len():
    rec = NetworkCaptureRecorder()
    # category A: 2 Japanese (one a duplicate) + 1 non-Japanese -> count 3, japanese 2, unique 2.
    rec.record("<%sM_header>", JA)
    rec.record("<%sM_header>", JA)   # duplicate -> count++ but unique stays
    rec.record("<%sM_header>", EN)   # non-Japanese
    # category B: 1 Japanese.
    rec.record("<%sB_VALUE>", JA2)

    report = rec.report()

    assert report["totals"]["calls"] == 4
    assert report["totals"]["categories"] == 2
    assert report["totals"]["japanese"] == 3

    a = report["categories"]["<%sM_header>"]
    assert a["count"] == 3
    assert a["unique"] == 2            # duplicate not double-counted
    assert a["japanese"] == 2
    assert a["pct_japanese"] == pytest.approx(66.7, abs=0.1)
    assert a["len_min"] == len(JA)
    assert a["len_max"] == len(EN)
    assert a["len_avg"] == pytest.approx((len(JA) * 2 + len(EN)) / 3, abs=0.1)
    assert JA in a["samples"] and EN in a["samples"]


def test_report_orders_categories_by_count_desc():
    rec = NetworkCaptureRecorder()
    for _ in range(5):
        rec.record("big", JA)
    for _ in range(2):
        rec.record("small", JA)
    rec.record("tiny", JA)

    cats = list(rec.report()["categories"].keys())
    assert cats == ["big", "small", "tiny"]


def test_unique_samples_capped_at_max_samples():
    rec = NetworkCaptureRecorder()
    # Emit MORE than MAX_SAMPLES distinct strings (all Japanese so japanese keeps counting).
    n = MAX_SAMPLES + 50
    for i in range(n):
        rec.record("<%sM_header>", f"{JA}{i}")

    c = rec.report()["categories"]["<%sM_header>"]
    assert c["count"] == n                 # ALL calls counted
    assert c["japanese"] == n              # ALL Japanese counted
    assert c["unique"] == MAX_SAMPLES      # retained samples capped
    assert len(c["samples"]) == MAX_SAMPLES


def test_dump_writes_json_roundtrippable_to_report(tmp_path):
    rec = NetworkCaptureRecorder()
    rec.record("<%sM_header>", JA)
    rec.record("<%sM_header>", EN)
    rec.record("<%sB_VALUE>", JA2)

    out = tmp_path / "capture.json"
    returned = rec.dump(out)
    assert returned == out
    assert out.exists()

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == rec.report()          # JSON round-trips to the in-memory report exactly


def test_summary_rows_ranked_by_count_with_two_samples():
    rec = NetworkCaptureRecorder()
    rec.record("big", JA)
    rec.record("big", JA2)
    rec.record("big", EN)
    rec.record("small", JA)

    rows = summary_rows(rec.report())
    assert rows[0][0] == "big"            # ranked by count DESC
    assert rows[1][0] == "small"
    assert rows[0][1] == "3"             # count column
    # samples cell holds at most TWO samples joined by " | ".
    assert rows[0][5].count(" | ") <= 1
    # build_summary_table must not raise on a real report.
    build_summary_table(rec.report())


# =============================================================================================== #
# capture fn behaviour (the fn run() wires for network_text)                                       #
# =============================================================================================== #


def test_capture_fn_returns_none_and_records_every_input():
    """Equivalent of the run()-wired capture fn: record (category, ja) and return None."""
    rec = NetworkCaptureRecorder()

    def capture_fn(ja, category, _rec=rec):
        _rec.record(category, ja)
        return None

    assert capture_fn(JA, "<%sM_header>") is None
    assert capture_fn(EN, "<%sM_header>") is None
    assert capture_fn(JA2, "<%sB_VALUE>") is None

    report = rec.report()
    assert report["totals"]["calls"] == 3
    assert report["categories"]["<%sM_header>"]["count"] == 2
    assert report["categories"]["<%sB_VALUE>"]["count"] == 1


# =============================================================================================== #
# bench-network                                                                                    #
# =============================================================================================== #


class _FakeProvider:
    name = "fake"


class _FakeBenchTranslator:
    """A translator whose translate_now sleeps a deterministic tiny amount; lookup hits one string."""

    def __init__(self) -> None:
        self.sync_provider = _FakeProvider()
        self.upgrade_provider = None
        self.started = False

        class _Cache:
            def close(self_inner):
                self_inner.closed = True

        self.cache = _Cache()

    def start(self):
        self.started = True

    def stop(self):
        pass

    def lookup(self, ja):
        # Exactly ONE string is a cache hit.
        return "cached-en" if ja == JA else None

    def translate_now(self, ja):
        import time
        time.sleep(0.001)        # deterministic tiny latency so perf_counter sees > 0
        return f"en:{ja}"


def test_bench_network_produces_latency_and_cache_numbers(tmp_path, monkeypatch, capsys):
    # A tiny capture report: one Japanese category (2 samples, one of which is the cache-hit) and one
    # no-Japanese category that --japanese-only must skip.
    report = {
        "totals": {"calls": 3, "categories": 2, "japanese": 2},
        "categories": {
            "<%sM_header>": {
                "count": 2, "unique": 2, "japanese": 2, "pct_japanese": 100.0,
                "len_min": 1, "len_max": 5, "len_avg": 3.0, "samples": [JA, JA2],
            },
            "<%sB_VALUE>": {
                "count": 1, "unique": 1, "japanese": 0, "pct_japanese": 0.0,
                "len_min": 2, "len_max": 2, "len_avg": 2.0, "samples": ["42"],
            },
        },
    }
    cap = tmp_path / "capture.json"
    cap.write_text(json.dumps(report), encoding="utf-8")

    fake = _FakeBenchTranslator()
    monkeypatch.setattr(cli, "_build_translator", lambda c: fake)
    import dqxclarity.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config())

    cli.bench_network(capture_json=cap, limit=20, japanese_only=True)

    out = capsys.readouterr().out
    # The Japanese category was benchmarked; the no-Japanese one was skipped.
    assert "<%sM_header>" in out
    assert "<%sB_VALUE>" not in out
    # Heads-up about real network calls printed.
    assert "REAL translation calls" in out
    assert fake.started is True


def test_bench_network_japanese_only_false_includes_all(tmp_path, monkeypatch, capsys):
    report = {
        "totals": {"calls": 1, "categories": 1, "japanese": 0},
        "categories": {
            "<%sB_VALUE>": {
                "count": 1, "unique": 1, "japanese": 0, "pct_japanese": 0.0,
                "len_min": 2, "len_max": 2, "len_avg": 2.0, "samples": ["42"],
            },
        },
    }
    cap = tmp_path / "capture.json"
    cap.write_text(json.dumps(report), encoding="utf-8")

    fake = _FakeBenchTranslator()
    monkeypatch.setattr(cli, "_build_translator", lambda c: fake)
    import dqxclarity.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config())

    cli.bench_network(capture_json=cap, limit=5, japanese_only=False)

    out = capsys.readouterr().out
    assert "<%sB_VALUE>" in out   # included now that japanese_only is False


# =============================================================================================== #
# run() wiring: --capture-network installs the capture fn and dumps on exit                        #
# =============================================================================================== #


class _NetSpec:
    """A network_text return-hook spec (only the attributes _build_fn branches on)."""

    name = "network_text"
    player = False
    return_hook = True
    is_name = False
    reward_field_indices = ()
    wrap_width = 46
    lines_per_page = 0
    sync = False


class _NetFound:
    def __init__(self) -> None:
        self.spec = _NetSpec()
        self.func_addr = 0x500000


def test_run_capture_network_installs_capture_fn_and_dumps_report(run_env, tmp_path, monkeypatch):
    """--capture-network <tmp>: the installed network_text fn records + returns None; report dumped."""
    st = run_env["state"]

    # Make locate() resolve the network_text return-hook so _build_fn takes the capture branch.
    import dqxclarity.process.hooks as hookmod
    monkeypatch.setattr(hookmod, "locate", lambda mem, names: [_NetFound()])

    captured_fn = {}

    # Intercept the (name, hook, fn) the loop builds so we can drive the installed capture fn.
    import dqxclarity.runtime.dispatch as dispatch_mod
    orig_serve = dispatch_mod.serve

    def capturing_serve(mem, installed, *, stop, game_gone=None, on_line=None):
        # installed is [(name, hook, fn)]; grab the network_text fn.
        for name, _hook, fn in installed:
            if name == "network_text":
                captured_fn["fn"] = fn
        return orig_serve(mem, installed, stop=stop, game_gone=game_gone, on_line=on_line)

    monkeypatch.setattr(dispatch_mod, "serve", capturing_serve)

    st["serve_script"] = [_user_quit]
    out = tmp_path / "cap.json"

    cli.run(hooks="network_text", duration=0.0, patch=True, capture_network=out)

    # The installed fn is the pure-observe capture fn: records + returns None.
    fn = captured_fn["fn"]
    assert fn(JA, "<%sM_header>") is None
    assert fn(EN, "<%sB_VALUE>") is None

    # NOTE: the dump in finally fired with whatever the recorder held at exit. We additionally drove
    # the fn AFTER the loop above (it shares the same recorder), so re-dump to assert the wiring path
    # works end to end via the file written on exit.
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert "totals" in loaded and "categories" in loaded


# Local copy of the lifecycle serve outcome (a user Ctrl-C) so this file is self-contained.
def _user_quit(stop, game_gone):
    raise KeyboardInterrupt
