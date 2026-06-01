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


def _patch_sync_sources(monkeypatch, *, community):
    """Stub _run_sync's sources: a dummy cache + every download mocked. `community` is the only
    source that succeeds (returns its value); all others raise a caught network error."""
    import httpx

    from dqxclarity.translate import community as community_mod
    from dqxclarity.translate import db as db_mod
    from dqxclarity.translate import glossary as glossary_mod

    class _DummyCache:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

        def __len__(self):
            return 0

    def _boom(*a, **k):
        raise httpx.HTTPError("network down")  # in every per-step handler's caught set

    monkeypatch.setattr(db_mod, "TranslationCache", _DummyCache)
    monkeypatch.setattr(community_mod, "sync_community", community)
    for name in ("sync_all_static", "sync_custom_supplements", "fetch_suppressions",
                 "fetch_reward_items"):
        monkeypatch.setattr(community_mod, name, _boom)
    monkeypatch.setattr(glossary_mod, "sync_glossary", _boom)


def test_run_sync_does_not_stamp_marker_on_total_failure(cfg_dir, monkeypatch):
    """Every source failing -> _run_sync returns False and does NOT stamp the marker, so the next
    run RETRIES instead of being fooled into thinking the DB is fresh for the whole max-age window."""
    from dqxclarity import cli

    def _boom(*a, **k):
        import httpx
        raise httpx.HTTPError("network down")

    _patch_sync_sources(monkeypatch, community=_boom)

    assert cli._run_sync(cfg_mod.Config()) is False
    assert not (cfg_dir / "last_sync").exists()  # NOT stamped


def test_run_sync_stamps_marker_when_a_source_succeeds(cfg_dir, monkeypatch):
    """At least one source downloading counts as success: returns True and stamps the marker (so
    the staleness check stays network-free until it next goes stale)."""
    from dqxclarity import cli

    _patch_sync_sources(monkeypatch, community=lambda cache: 42)  # only merge.xlsx succeeds

    assert cli._run_sync(cfg_mod.Config()) is True
    assert (cfg_dir / "last_sync").is_file()  # stamped
