"""Local-only DB freshness tracking for the staleness-gated auto-refresh (#19).

A single ``last_sync`` marker file in ``CONFIG_DIR`` records the epoch time of the most recent
successful ``sync``. The staleness check is PURELY LOCAL — it reads only this marker, never the
network — so a fresh DB adds ZERO startup cost to ``run``; only an actually-stale or never-synced
DB may trigger a network refresh. This mirrors the patch-currency pattern (``patch.auto_apply``).

Stored alongside ``clarity_cache.db`` / ``suppressions.json`` / ``reward_items.json`` so one
``dqxclarity sync`` refreshes the cache and stamps the marker in the same place.
"""

from __future__ import annotations

import os
import time

from .. import config as cfg_mod


def _marker_path():
    """Path to the last-sync marker (resolved live so a monkeypatched CONFIG_DIR is honoured)."""
    return cfg_mod.CONFIG_DIR / "last_sync"


def mark_synced(now: float | None = None) -> None:
    """Record ``now`` (default: current epoch time) as the last successful sync time (atomic)."""
    ts = time.time() if now is None else now
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(repr(float(ts)), encoding="utf-8")
    os.replace(tmp, path)


def last_sync_time() -> float | None:
    """Epoch time of the last sync, or None if never synced / the marker is missing or unreadable."""
    try:
        return float(_marker_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def db_age_days() -> float | None:
    """Days since the last sync, or None if never synced."""
    last = last_sync_time()
    if last is None:
        return None
    return (time.time() - last) / 86400.0


def is_db_stale(max_age_days: int) -> bool:
    """True if the DB has never been synced (no marker) OR is older than ``max_age_days``.

    PURELY LOCAL — reads only the marker file, never the network.
    """
    age = db_age_days()
    if age is None:
        return True  # never synced
    return age > max_age_days
