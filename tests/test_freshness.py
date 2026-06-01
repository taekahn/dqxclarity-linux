"""Tests for the LOCAL-ONLY DB freshness helpers behind the staleness-gated auto-refresh (#19).

CONFIG_DIR is monkeypatched to a tmp dir so the `last_sync` marker is read/written there. No
network is ever touched — that's the whole point of the local check.
"""

from __future__ import annotations

import time

import pytest

from dqxclarity import config as cfg_mod
from dqxclarity.translate import freshness


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    """Point the freshness marker at a throwaway dir (resolved live via cfg_mod.CONFIG_DIR)."""
    d = tmp_path / "data"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", d)
    return d


def test_marker_round_trip(cfg_dir):
    """mark_synced writes a marker that last_sync_time / db_age_days reflect."""
    before = time.time()
    freshness.mark_synced()
    after = time.time()

    last = freshness.last_sync_time()
    assert last is not None
    assert before <= last <= after
    # The marker file lives in CONFIG_DIR.
    assert (cfg_dir / "last_sync").is_file()

    age = freshness.db_age_days()
    assert age is not None
    assert 0.0 <= age < 1.0  # just synced -> well under a day


def test_mark_synced_accepts_explicit_time(cfg_dir):
    """An explicit epoch time is honoured (used so tests don't need to mock the clock)."""
    ten_days_ago = time.time() - 10 * 86400
    freshness.mark_synced(ten_days_ago)
    assert freshness.last_sync_time() == pytest.approx(ten_days_ago)
    assert freshness.db_age_days() == pytest.approx(10.0, abs=0.01)


def test_no_marker_means_never_synced(cfg_dir):
    """With no marker on disk, last_sync_time/db_age_days are None (no exception)."""
    assert not (cfg_dir / "last_sync").exists()
    assert freshness.last_sync_time() is None
    assert freshness.db_age_days() is None


def test_unreadable_marker_degrades_to_none(cfg_dir):
    """A corrupt (non-numeric) marker degrades to None rather than raising."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "last_sync").write_text("not-a-number", encoding="utf-8")
    assert freshness.last_sync_time() is None
    assert freshness.db_age_days() is None


def test_is_db_stale_no_marker_is_stale(cfg_dir):
    """Never synced -> stale (so a first run triggers the one-time refresh)."""
    assert freshness.is_db_stale(7) is True


def test_is_db_stale_fresh_marker_is_not_stale(cfg_dir):
    """A marker written 'now' -> not stale."""
    freshness.mark_synced()
    assert freshness.is_db_stale(7) is False


def test_is_db_stale_old_marker_is_stale(cfg_dir):
    """A marker written 10 days ago -> stale at max_age_days=7 (no clock mocking needed)."""
    freshness.mark_synced(time.time() - 10 * 86400)
    assert freshness.is_db_stale(7) is True
    # And NOT stale under a generous threshold.
    assert freshness.is_db_stale(30) is False
