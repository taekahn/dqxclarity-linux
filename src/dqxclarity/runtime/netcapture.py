"""Network-text traffic CAPTURE recorder + report rendering for the tiering decision.

The network_text return-hook surface routes every captured string by a category tag (e.g.
``<%sM_header>``, ``<%sM_kaisetubun>``). To decide which categories deserve a HOT/sync translate,
a COLD/async translate, or a DROP, we want to observe the FULL traffic of a real playthrough.

``NetworkCaptureRecorder.record(category, text)`` is the HOT-path hook: it is called once per
network_text call (battle volume), so it does ZERO disk I/O and only accumulates cheap per-category
counters + a capped set of unique sample strings. ``report()`` materializes the aggregate, ``dump()``
writes it as pretty JSON, and ``summary_rows()`` / a rendered table surface the ranking.

The recorder is accessed only from the single serve thread's record() calls and dumped once after the
serve loop, so it needs NO locking (matching how the CLI drives it). It is pure/unit-testable — no
game, network, or process access.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .dispatch import is_japanese

# Cap the number of UNIQUE sample strings kept per category so memory stays bounded at battle volume
# (a busy category can emit thousands of distinct strings; we only need a representative sample).
MAX_SAMPLES = 200


class _CatStats:
    """Per-category accumulator. All updates are O(1) on the hot path (set membership + counters)."""

    __slots__ = ("count", "japanese", "len_min", "len_max", "len_sum", "samples")

    def __init__(self) -> None:
        self.count = 0
        self.japanese = 0
        self.len_min: int | None = None
        self.len_max: int | None = None
        self.len_sum = 0
        # A bounded set of UNIQUE sample strings (capped at MAX_SAMPLES). A set keeps uniqueness +
        # membership cheap; once full we stop adding (the count/japanese/len stats keep accumulating
        # over ALL calls, only the retained samples are capped).
        self.samples: set[str] = set()

    def record(self, text: str) -> None:
        self.count += 1
        n = len(text)
        self.len_sum += n
        if self.len_min is None or n < self.len_min:
            self.len_min = n
        if self.len_max is None or n > self.len_max:
            self.len_max = n
        if is_japanese(text):
            self.japanese += 1
        # Cap retained unique samples; membership test is cheap and bounds memory.
        if len(self.samples) < MAX_SAMPLES:
            self.samples.add(text)


class NetworkCaptureRecorder:
    """Accumulates network_text traffic per category for the hot/cold/drop tiering decision.

    Single-threaded by contract (the serve loop calls ``record`` from one thread; ``report``/``dump``
    run after the loop), so there is no locking. ``record`` does no disk I/O.
    """

    def __init__(self) -> None:
        self._cats: dict[str, _CatStats] = {}
        self._calls = 0

    def record(self, category: str, text: str) -> None:
        """Hot path: tally one network_text call. CHEAP — no I/O, O(1) per call."""
        self._calls += 1
        stats = self._cats.get(category)
        if stats is None:
            stats = _CatStats()
            self._cats[category] = stats
        stats.record(text)

    def report(self) -> dict:
        """Materialize the aggregate report. Categories are ordered by call count DESC."""
        total_japanese = sum(s.japanese for s in self._cats.values())
        ordered = sorted(self._cats.items(), key=lambda kv: kv[1].count, reverse=True)
        categories: dict[str, dict] = {}
        for cat, s in ordered:
            unique = len(s.samples)
            pct_japanese = (s.japanese / s.count * 100.0) if s.count else 0.0
            len_avg = (s.len_sum / s.count) if s.count else 0.0
            categories[cat] = {
                "count": s.count,
                "unique": unique,
                "japanese": s.japanese,
                "pct_japanese": round(pct_japanese, 1),
                "len_min": s.len_min if s.len_min is not None else 0,
                "len_max": s.len_max if s.len_max is not None else 0,
                "len_avg": round(len_avg, 1),
                # Sort samples for a stable, deterministic report (set order is otherwise arbitrary).
                "samples": sorted(s.samples),
            }
        return {
            "totals": {
                "calls": self._calls,
                "categories": len(self._cats),
                "japanese": total_japanese,
            },
            "categories": categories,
        }

    def dump(self, path: Path | str) -> Path:
        """Write ``report()`` as pretty JSON to ``path`` (atomic write). Returns the Path."""
        path = Path(path)
        report = self.report()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path


def _truncate(text: str, width: int = 40) -> str:
    """One-line, length-capped rendering of a sample string for the summary table."""
    line = text.replace("\n", " ").replace("\r", " ")
    if len(line) > width:
        line = line[: width - 1] + "…"
    return line


def summary_rows(report: dict) -> list[tuple[str, str, str, str, str, str]]:
    """Build the per-category summary rows (category, count, uniq, %JP, avg_len, samples).

    Takes a ``report()`` dict (kept OUT of the recorder so rendering has no hot-path cost) and returns
    string rows ranked by count DESC (report() already orders the categories that way). The last
    column holds up to TWO truncated samples joined by ``" | "``.
    """
    rows: list[tuple[str, str, str, str, str, str]] = []
    for cat, c in report.get("categories", {}).items():
        samples = c.get("samples", [])[:2]
        sample_cell = " | ".join(_truncate(s) for s in samples)
        rows.append((
            cat,
            str(c["count"]),
            str(c["unique"]),
            f"{c['pct_japanese']:.1f}",
            f"{c['len_avg']:.1f}",
            sample_cell,
        ))
    return rows


def build_summary_table(report: dict):
    """Build a rich Table of the capture summary, ranked by count DESC. Import-light (lazy rich)."""
    from rich.table import Table

    totals = report.get("totals", {})
    table = Table(
        title=(
            f"network_text capture — {totals.get('calls', 0)} calls, "
            f"{totals.get('categories', 0)} categories, "
            f"{totals.get('japanese', 0)} japanese"
        )
    )
    table.add_column("category", style="cyan", no_wrap=True)
    table.add_column("count", justify="right")
    table.add_column("uniq", justify="right")
    table.add_column("%JP", justify="right")
    table.add_column("avg_len", justify="right")
    table.add_column("samples")
    for row in summary_rows(report):
        table.add_row(*row)
    return table
