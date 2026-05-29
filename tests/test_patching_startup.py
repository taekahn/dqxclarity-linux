"""Tests for `dqxclarity run`'s startup auto-patch ritual.

The decision logic lives in two cli helpers (`_apply_patches_for_run`, `_wait_for_game`) plus
the small branch at the top of `run`. We unit-test those directly, monkeypatching the process
probes (`find_game_pid`, `patch_files.is_game_running`) and the patch engine
(`patch_files.apply`) so nothing touches a real game, a real install, or the network.

The four behavioural contracts (from the feature spec):
  (a) game DOWN  -> apply() called with force=False, dry_run=False, then we wait for the game.
  (b) game UP    -> apply() NOT applied for real; a dry_run=True staleness probe runs; attach
                    proceeds immediately (no wait).
  (c) --no-patch / auto_apply False -> the patch step is SKIPPED, but the supervisory loop STILL
                    waits for the game (wait-from-patch is decoupled by the lifecycle epic; the old
                    fail-fast Exit(1) is gone).
  (d) Ctrl-C in the wait loop -> clean typer.Exit(0), no traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from dqxclarity import cli
from dqxclarity import config as cfg_mod
from dqxclarity.patching import files as patch_files
from dqxclarity.patching.manifest import Manifest
from dqxclarity.process.discover import GameInstall


# --------------------------------------------------------------------------- helpers/fixtures


def _valid_install(tmp_path: Path) -> GameInstall:
    """A GameInstall whose game_exe + data_dir exist so looks_valid() is True."""
    root = tmp_path / "DRAGON QUEST X"
    (root / "Game" / "Content" / "Data").mkdir(parents=True)
    (root / "Game" / "DQXGame.exe").write_bytes(b"MZ")
    inst = GameInstall(install_root=root)
    assert inst.looks_valid()
    return inst


class _FakeManifest:
    name = "test"
    version = "1"

    def has_files(self) -> bool:
        return True


@pytest.fixture
def patched_env(tmp_path, monkeypatch):
    """Stub out install discovery + manifest loading so _apply_patches_for_run is hermetic.

    Records every patch_files.apply(...) call so tests can assert force/dry_run. The default
    apply stub returns an empty (up-to-date) summary; tests override `apply_result` as needed.
    """
    inst = _valid_install(tmp_path)
    monkeypatch.setattr(cli, "_resolve_install", lambda cfg: inst)
    monkeypatch.setattr(cli, "load_manifest", lambda src: _FakeManifest())

    calls: list[dict] = []
    state = {"result": {"installed": [], "skipped_current": []}}

    def fake_apply(install, manifest, *, requested_groups, cache_dir, backup_dir, force, dry_run):
        calls.append({"force": force, "dry_run": dry_run, "groups": set(requested_groups)})
        res = dict(state["result"])
        if dry_run:
            res.setdefault("would_install", state.get("would_install", []))
        return res

    monkeypatch.setattr(cli.patch_files, "apply", fake_apply)
    return {"install": inst, "calls": calls, "state": state}


def _cfg(auto_apply=True):
    c = cfg_mod.Config()
    c.patch.auto_apply = auto_apply
    return c


# --------------------------------------------------------------------------- (a) game DOWN


def test_apply_for_run_game_down_applies_for_real(patched_env, monkeypatch):
    """Game not running -> apply() runs ONCE with force=False, dry_run=False (a real install)."""
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: False)
    patched_env["state"]["result"] = {"installed": ["a", "b"], "skipped_current": ["c"]}

    cli._apply_patches_for_run(_cfg())

    calls = patched_env["calls"]
    assert len(calls) == 1
    assert calls[0]["force"] is False
    assert calls[0]["dry_run"] is False
    assert "game_files" in calls[0]["groups"]


def test_run_game_down_applies_then_waits(patched_env, monkeypatch):
    """`run --patch` with the game closed: apply runs, then we wait for the game to appear."""
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: False)

    applied = {"called": False}
    monkeypatch.setattr(cli, "_apply_patches_for_run",
                        lambda cfg: applied.__setitem__("called", True))
    # Game is down at the top; _wait_for_game is the path that yields the pid.
    monkeypatch.setattr(cli, "find_game_pid", lambda: None)
    waited = {"called": False}

    def fake_wait(poll=1.5):
        waited["called"] = True
        return 4321

    monkeypatch.setattr(cli, "_wait_for_game", fake_wait)
    monkeypatch.setattr(cfg_mod, "load", lambda: _cfg(auto_apply=True))

    # Stop run() right after pid resolution so we don't drive the real attach machinery.
    sentinel = RuntimeError("reached-attach")

    def boom(*a, **k):
        raise sentinel

    monkeypatch.setattr(cli.hookjournal, "recover_orphans", boom)

    with pytest.raises(RuntimeError) as ei:
        cli.run(hooks="dialogue", duration=0.0, patch=True)
    assert ei.value is sentinel
    assert applied["called"] is True
    assert waited["called"] is True


# --------------------------------------------------------------------------- (b) game UP


def test_apply_for_run_game_up_probes_only(patched_env, monkeypatch):
    """Game running -> apply() runs ONLY as a dry_run probe; never a real install."""
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: True)
    patched_env["state"]["would_install"] = ["Game/Content/Data/data00000000.win32.dat1"]

    cli._apply_patches_for_run(_cfg())

    calls = patched_env["calls"]
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    assert calls[0]["force"] is False
    # No second, real apply happened.
    assert not any(c["dry_run"] is False for c in calls)


def test_apply_for_run_game_up_probe_error_swallowed(patched_env, monkeypatch):
    """A staleness-probe error must never propagate (never block attaching)."""
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: True)

    def boom_apply(*a, **k):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(cli.patch_files, "apply", boom_apply)
    # Must return cleanly, not raise.
    cli._apply_patches_for_run(_cfg())


def test_run_game_up_skips_wait_and_attaches(patched_env, monkeypatch):
    """`run --patch` with the game already up: no wait loop, attach proceeds immediately."""
    applied = {"called": False}
    monkeypatch.setattr(cli, "_apply_patches_for_run",
                        lambda cfg: applied.__setitem__("called", True))
    monkeypatch.setattr(cli, "find_game_pid", lambda: 999)

    def no_wait(poll=1.5):
        raise AssertionError("must not wait when the game is already running")

    monkeypatch.setattr(cli, "_wait_for_game", no_wait)
    monkeypatch.setattr(cfg_mod, "load", lambda: _cfg(auto_apply=True))

    sentinel = RuntimeError("reached-attach")
    monkeypatch.setattr(cli.hookjournal, "recover_orphans",
                        lambda *a, **k: (_ for _ in ()).throw(sentinel))

    with pytest.raises(RuntimeError) as ei:
        cli.run(hooks="dialogue", duration=0.0, patch=True)
    assert ei.value is sentinel
    assert applied["called"] is True  # probe/apply helper still consulted


# --------------------------------------------------------------------------- (c) opted out


def test_run_no_patch_flag_skips_patch_but_still_waits(patched_env, monkeypatch):
    """--no-patch: the patch helper does NOT run, but we STILL wait for the game (no fail-fast).

    Decoupled wait-from-patch (lifecycle epic): the supervisory loop always waits for the game, so
    --no-patch only skips the (unsafe-on-live-files) patch step. The game-down path must reach
    _wait_for_game instead of the old Exit(1).
    """
    helper = {"called": False}
    monkeypatch.setattr(cli, "_apply_patches_for_run",
                        lambda cfg: helper.__setitem__("called", True))
    waited = {"called": False}

    def fake_wait(poll=1.5):
        waited["called"] = True
        return 4321

    monkeypatch.setattr(cli, "_wait_for_game", fake_wait)
    monkeypatch.setattr(cli, "find_game_pid", lambda: None)
    monkeypatch.setattr(cfg_mod, "load", lambda: _cfg(auto_apply=True))

    # Stop run() right after pid resolution so we don't drive the real attach machinery.
    sentinel = RuntimeError("reached-attach")
    monkeypatch.setattr(cli.hookjournal, "recover_orphans",
                        lambda *a, **k: (_ for _ in ()).throw(sentinel))

    with pytest.raises(RuntimeError) as ei:
        cli.run(hooks="dialogue", duration=0.0, patch=False)
    assert ei.value is sentinel
    assert helper["called"] is False  # patch step skipped (--no-patch)
    assert waited["called"] is True   # but we STILL waited for the game


def test_run_auto_apply_false_skips_patch_but_still_waits(patched_env, monkeypatch):
    """config patch.auto_apply False: skips patch, but still waits (same decoupled behaviour)."""
    helper = {"called": False}
    monkeypatch.setattr(cli, "_apply_patches_for_run",
                        lambda cfg: helper.__setitem__("called", True))
    waited = {"called": False}

    def fake_wait(poll=1.5):
        waited["called"] = True
        return 4321

    monkeypatch.setattr(cli, "_wait_for_game", fake_wait)
    monkeypatch.setattr(cli, "find_game_pid", lambda: None)
    monkeypatch.setattr(cfg_mod, "load", lambda: _cfg(auto_apply=False))

    sentinel = RuntimeError("reached-attach")
    monkeypatch.setattr(cli.hookjournal, "recover_orphans",
                        lambda *a, **k: (_ for _ in ()).throw(sentinel))

    with pytest.raises(RuntimeError) as ei:
        cli.run(hooks="dialogue", duration=0.0, patch=True)
    assert ei.value is sentinel
    assert helper["called"] is False  # patch step skipped (auto_apply off)
    assert waited["called"] is True   # but we STILL waited for the game


# --------------------------------------------------------------------------- (d) Ctrl-C in wait


def test_wait_for_game_returns_pid_when_found(monkeypatch):
    """The wait loop returns as soon as find_game_pid yields a pid (no sleeping needed)."""
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    # poll=0 keeps it instant even if it had to loop.
    assert cli._wait_for_game(poll=0) == 1234


def test_wait_for_game_polls_until_game_appears(monkeypatch):
    """The loop keeps polling while the game is absent, then returns the pid once it shows up."""
    seq = iter([None, None, 777])
    monkeypatch.setattr(cli, "find_game_pid", lambda: next(seq))
    # _wait_for_game imports `time` locally; patch the stdlib module's sleep so the test is instant.
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda *_a, **_k: None)
    assert cli._wait_for_game(poll=0) == 777


def test_wait_for_game_keyboardinterrupt_exits_cleanly(monkeypatch):
    """Ctrl-C during the wait -> typer.Exit(0), never a bare KeyboardInterrupt/traceback."""
    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "find_game_pid", interrupt)
    with pytest.raises(typer.Exit) as ei:
        cli._wait_for_game(poll=0)
    assert ei.value.exit_code == 0


# --------------------------------------------------------------------------- best-effort guards


def test_apply_for_run_no_install_is_a_noop(patched_env, monkeypatch):
    """No locatable install -> patch step skipped, apply never called (must not abort run)."""
    monkeypatch.setattr(cli, "_resolve_install", lambda cfg: None)
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: False)
    cli._apply_patches_for_run(_cfg())
    assert patched_env["calls"] == []


def test_apply_for_run_empty_manifest_is_a_noop(patched_env, monkeypatch):
    """Manifest with no files -> patch step skipped, apply never called."""
    class _Empty(_FakeManifest):
        def has_files(self):
            return False

    monkeypatch.setattr(cli, "load_manifest", lambda src: _Empty())
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: False)
    cli._apply_patches_for_run(_cfg())
    assert patched_env["calls"] == []


# --------------------------------------------------------------------------- TOCTOU race fix


def test_run_resolves_pid_with_single_probe_no_race(patched_env, monkeypatch):
    """The game-up path must read find_game_pid ONCE.

    Regression guard for the TOCTOU race: the old code probed find_game_pid twice (check then
    fetch). If the game exited between the two calls, the second returned None and was handed to
    LinuxProcessMemory(None). Here find_game_pid yields a real pid on the first call and None on
    every call after — the run must still attach with the first (real) pid, proving it does not
    re-probe.
    """
    monkeypatch.setattr(cli, "_apply_patches_for_run", lambda cfg: None)
    seq = iter([4242])  # one pid, then StopIteration -> None means "would have raced to None"

    def flaky_pid():
        return next(seq, None)

    monkeypatch.setattr(cli, "find_game_pid", flaky_pid)
    monkeypatch.setattr(cli, "_wait_for_game",
                        lambda poll=1.5: (_ for _ in ()).throw(
                            AssertionError("must not wait: first probe already returned a pid")))
    monkeypatch.setattr(cfg_mod, "load", lambda: _cfg(auto_apply=True))

    captured = {"pid": None}

    def capture_pid(*a, **k):
        captured["pid"] = a[1] if len(a) > 1 else k.get("pid")
        raise RuntimeError("reached-attach")

    # recover_orphans(mem, pid) is the first thing after pid resolution; capture the pid it sees.
    monkeypatch.setattr(cli.hookjournal, "recover_orphans", capture_pid)
    # LinuxProcessMemory is imported lazily inside run(); stub it so a fake pid doesn't touch /proc.
    import dqxclarity.process.memory_linux as mem_mod
    monkeypatch.setattr(mem_mod, "LinuxProcessMemory", lambda pid: {"pid": pid})

    with pytest.raises(RuntimeError, match="reached-attach"):
        cli.run(hooks="dialogue", duration=0.0, patch=True)
    # The pid threaded into attach is the first (real) probe value, never a raced None.
    assert captured["pid"] == 4242


# --------------------------------------------------------------------------- OSError on real apply


def test_apply_for_run_real_apply_oserror_is_swallowed(patched_env, monkeypatch, capsys):
    """A filesystem OSError from the real apply (read-only dir / full disk) must not abort run.

    _download/_atomic_install do mkdir/copy2/os.replace, all of which can raise OSError. The
    helper must catch it, print a 'patch step skipped' note, and return so run() still attaches.
    """
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: False)

    def boom_oserror(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(cli.patch_files, "apply", boom_oserror)
    # Must NOT raise.
    cli._apply_patches_for_run(_cfg())
    out = capsys.readouterr().out
    assert "patch step skipped" in out


# --------------------------------------------------------------------------- staleness warning


def test_apply_for_run_game_up_no_warning_when_up_to_date(patched_env, monkeypatch, capsys):
    """Game up + probe reports nothing stale -> NO false 'stale' warning is printed.

    This is the false-alarm fix at the cli boundary: when the dry-run probe returns an empty
    would_install (everything current), the user must not see the yellow staleness warning.
    """
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: True)
    patched_env["state"]["would_install"] = []  # nothing stale

    cli._apply_patches_for_run(_cfg())
    out = capsys.readouterr().out
    assert "may need updating" not in out
    assert "stale" not in out


def test_apply_for_run_game_up_warns_only_for_reported_stale(patched_env, monkeypatch, capsys):
    """Game up + probe reports stale files -> warning prints the reported count + verify hint."""
    monkeypatch.setattr(cli.patch_files, "is_game_running", lambda: True)
    patched_env["state"]["would_install"] = ["Game/Content/Data/data00000000.win32.dat1"]

    cli._apply_patches_for_run(_cfg())
    out = capsys.readouterr().out
    assert "1 patch file(s) may need updating" in out
    assert "patch --dry-run" in out


# --------------------------------------------------------------------------- files.py dry-run fix


def _real_manifest() -> Manifest:
    """Two game_files entries (idx already-existing target + dat1 new target)."""
    return Manifest.from_dict(
        {
            "name": "test",
            "version": "1",
            "groups": {
                "game_files": {
                    "optional": False,
                    "files": [
                        {"target": "Game/Content/Data/data00000000.win32.idx", "url": "u://idx"},
                        {"target": "Game/Content/Data/data00000000.win32.dat1", "url": "u://dat1"},
                    ],
                },
            },
        }
    )


def _real_install(tmp_path) -> GameInstall:
    root = tmp_path / "DRAGON QUEST X"
    (root / "Game" / "Content" / "Data").mkdir(parents=True)
    (root / "Game" / "DQXGame.exe").write_bytes(b"MZ")
    return GameInstall(install_root=root)


def test_files_dry_run_omits_files_matching_cache(tmp_path, monkeypatch):
    """The real fix: dry_run reports ONLY genuinely-stale files, not every planned file.

    When the cached asset (from a prior apply) is byte-identical to the installed file, that file
    is up to date and must be omitted from would_install — killing the false staleness alarm.
    """
    monkeypatch.setattr(patch_files, "is_game_running", lambda: False)
    inst = _real_install(tmp_path)
    data = inst.install_root / "Game" / "Content" / "Data"
    cache = tmp_path / "cache"
    cache.mkdir()

    # idx: installed file matches its cached asset -> CURRENT (omitted).
    (data / "data00000000.win32.idx").write_bytes(b"CURRENT-IDX")
    (cache / "data00000000.win32.idx").write_bytes(b"CURRENT-IDX")
    # dat1: not installed at all (and no cache) -> stale (reported).

    def _no_download(*a, **k):
        raise AssertionError("dry_run must never download")

    monkeypatch.setattr(patch_files, "_download", _no_download)

    summary = patch_files.apply(
        inst, _real_manifest(), requested_groups={"game_files"},
        cache_dir=cache, backup_dir=tmp_path / "backups", force=False, dry_run=True,
    )
    assert summary["dry_run"] is True
    # Only the genuinely-missing dat1 is reported; the cache-matching idx is omitted.
    assert summary["would_install"] == ["Game/Content/Data/data00000000.win32.dat1"]
    # Touched nothing: no cache writes beyond what we seeded, no backups, no installs.
    assert summary["installed"] == []
    assert not (tmp_path / "backups").exists()


def test_files_dry_run_reports_stale_when_cache_differs_or_absent(tmp_path, monkeypatch):
    """A changed installed file (cache mismatch) and a no-cache file are both reported stale."""
    monkeypatch.setattr(patch_files, "is_game_running", lambda: False)
    inst = _real_install(tmp_path)
    data = inst.install_root / "Game" / "Content" / "Data"
    cache = tmp_path / "cache"
    cache.mkdir()

    # idx: installed differs from cached asset -> stale.
    (data / "data00000000.win32.idx").write_bytes(b"OLD-IDX")
    (cache / "data00000000.win32.idx").write_bytes(b"NEW-IDX")
    # dat1: installed but NO cached asset -> unverifiable, conservatively stale.
    (data / "data00000000.win32.dat1").write_bytes(b"SOME-DAT1")

    monkeypatch.setattr(patch_files, "_download",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))

    summary = patch_files.apply(
        inst, _real_manifest(), requested_groups={"game_files"},
        cache_dir=cache, backup_dir=tmp_path / "backups", force=False, dry_run=True,
    )
    assert set(summary["would_install"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }


def test_files_dry_run_no_cache_reports_all_and_creates_no_cache(tmp_path, monkeypatch):
    """With no cache dir at all (fresh machine), every planned file is reported and the cache
    dir is NOT created — preserving the 'dry_run touches nothing' contract."""
    monkeypatch.setattr(patch_files, "is_game_running", lambda: False)
    inst = _real_install(tmp_path)
    data = inst.install_root / "Game" / "Content" / "Data"
    (data / "data00000000.win32.idx").write_bytes(b"EXISTING-IDX")
    cache = tmp_path / "cache"  # deliberately not created

    monkeypatch.setattr(patch_files, "_download",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))

    summary = patch_files.apply(
        inst, _real_manifest(), requested_groups={"game_files"},
        cache_dir=cache, backup_dir=tmp_path / "backups", force=False, dry_run=True,
    )
    assert set(summary["would_install"]) == {
        "Game/Content/Data/data00000000.win32.idx",
        "Game/Content/Data/data00000000.win32.dat1",
    }
    assert not cache.exists()
