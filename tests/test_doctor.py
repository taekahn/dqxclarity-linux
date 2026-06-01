"""Tests for the doctor translation-DB freshness check (#19).

Only the freshness Check is exercised here; install/ptrace/etc. are left to their own coverage.
CONFIG_DIR is redirected to a tmp dir so the `last_sync` marker is local to the test, and the
install discovery is stubbed so run_checks() doesn't depend on a real game.
"""

from __future__ import annotations

import time

import pytest

from dqxclarity import config as cfg_mod
from dqxclarity import doctor as doctor_mod


@pytest.fixture()
def doctor_env(tmp_path, monkeypatch):
    """Hermetic doctor: local marker dir + stubbed config + stubbed install/pid probes."""
    d = tmp_path / "data"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", d)
    monkeypatch.setattr(doctor_mod.cfg_mod, "load", lambda: cfg_mod.Config())
    monkeypatch.setattr(doctor_mod, "discover", lambda root: None)
    monkeypatch.setattr(doctor_mod, "find_game_pid", lambda: None)
    return d


def _db_check(checks):
    return next(c for c in checks if c.name == "translation DB")


def test_doctor_never_synced(doctor_env):
    checks, _ = doctor_mod.run_checks()
    c = _db_check(checks)
    assert c.ok is False
    assert "never synced" in c.detail
    assert "dqxclarity sync" in c.detail


def test_doctor_fresh(doctor_env):
    from dqxclarity.translate import freshness
    freshness.mark_synced()
    checks, _ = doctor_mod.run_checks()
    c = _db_check(checks)
    assert c.ok is True
    assert "synced" in c.detail and "days ago" in c.detail


def test_doctor_stale(doctor_env):
    from dqxclarity.translate import freshness
    freshness.mark_synced(time.time() - 10 * 86400)  # 10 days old, default max age 7
    checks, _ = doctor_mod.run_checks()
    c = _db_check(checks)
    assert c.ok is False
    assert "stale" in c.detail
    assert "dqxclarity sync" in c.detail
