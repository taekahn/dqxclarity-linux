"""Opt-in timing profiler for ``run --profile`` — pinpoints periodic game-thread hitches.

A perceptible in-game stutter is a game-thread STALL: a blocking hook's ``serve_once`` taking too
long (the game spins in the code cave until we write the result back), or the polling serve loop
being starved of CPU/GIL by a heavy name-scanner pass so it answers the game's request late. This
records three signals so the cause is identified by measurement, not assumption:

  * ``serve:<surface>``  — how long a ``serve_once`` took when it did real work (the direct block).
  * ``namescan:<warm|full>`` — how long each name-scanner pass took (the prime starvation suspect).
  * ``loop:serve`` — the gap between serve-loop iterations; a big gap means the loop was starved
    (e.g. the scanner held the GIL through a heavy scan) and the game waited that long for a reply.

Events at/above :data:`SLOW_S` are kept with a monotonic timestamp so a periodic spike's CADENCE is
visible (e.g. a ~1.9 s ``namescan:warm`` every ~3 s). Everything is also aggregated for an exit
summary. Cost when disabled is nil: the loops take ``profiler=None`` and skip all timing.
"""

from __future__ import annotations

import time

from rich.table import Table

SLOW_S = 0.03  # 30 ms: an event at/above this is a perceptible hitch -> kept in the timeline.


class Profiler:
    """Accumulates timed events. Thread-safe enough for this use (CPython list/dict ops are atomic)."""

    def __init__(self, on_slow=None) -> None:
        self._t0 = time.monotonic()
        self._on_slow = on_slow  # optional callback(ts_s, kind, label, ms, detail) for live logging
        self.agg: dict[str, list] = {}  # "kind:label" -> [count, total_s, max_s]
        self.slow: list[tuple] = []  # (ts_s, kind, label, dur_s, detail) for events >= SLOW_S
        # Set by the name-scanner thread while a pass is in flight; read by the serve thread when it
        # records a starvation gap, so each gap is ATTRIBUTED ("during a scan" vs "scan idle"). A
        # plain bool read/write is atomic under the GIL — no lock needed for this coarse attribution.
        self.scanning = False

    def elapsed(self) -> float:
        """Seconds since this profiler was created (for per-hook request-rate reporting)."""
        return time.monotonic() - self._t0

    def record(self, kind: str, label: str, dur_s: float, detail: str = "") -> None:
        key = f"{kind}:{label}"
        a = self.agg.get(key)
        if a is None:
            a = self.agg[key] = [0, 0.0, 0.0]
        a[0] += 1
        a[1] += dur_s
        if dur_s > a[2]:
            a[2] = dur_s
        if dur_s >= SLOW_S:
            ts = time.monotonic() - self._t0
            self.slow.append((ts, kind, label, dur_s, detail))
            if self._on_slow is not None:
                self._on_slow(ts, kind, label, dur_s * 1000.0, detail)

    def summary_rows(self) -> list[tuple]:
        """(key, count, total_ms, mean_ms, max_ms), sorted by total time descending."""
        rows = []
        for key, (n, tot, mx) in self.agg.items():
            rows.append((key, n, tot * 1000.0, (tot / n * 1000.0) if n else 0.0, mx * 1000.0))
        rows.sort(key=lambda r: r[2], reverse=True)
        return rows

    def summary_table(self) -> Table:
        t = Table(title="profile — by component (sorted by total blocking time)")
        t.add_column("component")
        t.add_column("count", justify="right")
        t.add_column("total ms", justify="right")
        t.add_column("mean ms", justify="right")
        t.add_column("max ms", justify="right")
        for key, n, tot, mean, mx in self.summary_rows():
            t.add_row(key, str(n), f"{tot:.0f}", f"{mean:.1f}", f"{mx:.0f}")
        return t

    def cadence_hint(self) -> str | None:
        """If the slow timeline shows a roughly-regular beat, report it (median gap + dominant label)."""
        if len(self.slow) < 3:
            return None
        ts = [e[0] for e in self.slow]
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        median_gap = gaps[len(gaps) // 2]
        # dominant label among slow events
        counts: dict[str, int] = {}
        for _ts, kind, label, _d, _x in self.slow:
            counts[f"{kind}:{label}"] = counts.get(f"{kind}:{label}", 0) + 1
        dominant = max(counts, key=counts.get)
        return f"~{median_gap:.1f}s between hitches; most common: {dominant} ({counts[dominant]}x)"
