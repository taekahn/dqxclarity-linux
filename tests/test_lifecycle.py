"""Tests for the game-lifecycle resilience added to `dqxclarity run`.

Three behaviours under test (the "survive the game closing/restarting" feature):

  1. ``serve()`` game-gone path — a hook whose ``serve_once`` raises ``struct.error`` while the
     pid is GONE (``mem.is_alive()`` False) makes serve set ``game_gone`` + ``stop`` and return
     cleanly (no traceback) so the supervisor can re-attach.
  2. ``serve()`` transient path — a one-off bad read on a STILL-ALIVE game (``is_alive`` True) is
     swallowed (skip that hook this tick) and serving continues; a later success is still served.
  3. ``run()`` supervisory re-attach loop — game-gone re-attaches (installs hooks again) then a
     user quit exits; a KeyboardInterrupt exits WITHOUT re-attaching; patch runs ONLY on the first
     attach; ``--no-patch`` still waits for the game.

Plus: ``files.py`` backup pruning keeps the newest N sets for the manifest and never touches
unrelated dirs.

Everything is mocked: no real game, /proc, network, or install is touched.
"""

from __future__ import annotations

import struct
import threading
import time
from pathlib import Path

import pytest
import typer

from dqxclarity import cli
from dqxclarity import config as cfg_mod
from dqxclarity.patching import files as patch_files
from dqxclarity.runtime.dispatch import serve


# =============================================================================================== #
# serve(): game-gone vs transient-blip                                                            #
# =============================================================================================== #


class _AliveMem:
    """A mem stub with a settable ``is_alive`` answer (the only surface serve() probes on error)."""

    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _Hook:
    """A serve_once stub driven by a script of behaviours: 'raise' / value / 'raise-os'."""

    def __init__(self, script) -> None:
        self._script = list(script)
        self.calls = 0

    def serve_once(self, mem, fn):
        self.calls += 1
        if not self._script:
            return None
        action = self._script.pop(0)
        if action == "raise":
            raise struct.error("unpack requires a buffer of 4 bytes")
        if action == "raise-os":
            raise OSError("bad read")
        return action


def test_serve_game_gone_sets_event_stops_and_returns_cleanly():
    """serve_once raises while the game is GONE -> game_gone set, stop set, clean return."""
    mem = _AliveMem(alive=False)
    hook = _Hook(["raise"])
    stop = threading.Event()
    game_gone = threading.Event()

    served = serve(mem, [("dialogue", hook, lambda j: j)], stop=stop, game_gone=game_gone)

    assert game_gone.is_set()  # the supervisor's re-attach signal
    assert stop.is_set()       # serve broke its own loop
    assert served == 0         # nothing was served before the game vanished
    # No exception propagated (the whole point): if it had, we'd never reach here.


def test_serve_game_gone_on_oserror_also_re_attaches():
    """An OSError (proc/<pid>/mem fallback) while gone is treated the same as struct.error."""
    mem = _AliveMem(alive=False)
    hook = _Hook(["raise-os"])
    stop = threading.Event()
    game_gone = threading.Event()

    serve(mem, [("quest", hook, lambda j: j)], stop=stop, game_gone=game_gone)

    assert game_gone.is_set()
    assert stop.is_set()


def test_serve_game_gone_without_game_gone_event_still_stops_cleanly():
    """game_gone is optional: when None, serve still stops cleanly on a gone game (no crash)."""
    mem = _AliveMem(alive=False)
    hook = _Hook(["raise"])
    stop = threading.Event()

    served = serve(mem, [("dialogue", hook, lambda j: j)], stop=stop, game_gone=None)

    assert stop.is_set()
    assert served == 0


def test_serve_transient_blip_is_skipped_and_loop_continues():
    """A one-off bad read on a LIVE game is swallowed; the next-tick success is still served."""
    mem = _AliveMem(alive=True)
    # First serve_once raises (blip), then returns a value (success), then we stop.
    hook = _Hook(["raise", "translated-line"])
    stop = threading.Event()
    game_gone = threading.Event()

    served_lines: list[tuple[str, str]] = []

    def on_line(name, ja):
        served_lines.append((name, ja))
        stop.set()  # stop right after the first real success so the test is bounded

    served = serve(
        mem, [("dialogue", hook, lambda j: j)], stop=stop, game_gone=game_gone, on_line=on_line
    )

    assert not game_gone.is_set()  # a live game never triggers re-attach
    assert served == 1             # the later success was served despite the earlier blip
    assert served_lines == [("dialogue", "translated-line")]
    assert hook.calls == 2         # raised once, succeeded once


def test_serve_transient_blip_does_not_set_stop():
    """A transient blip must NOT stop the serve loop on its own (only the test's own stop does)."""
    mem = _AliveMem(alive=True)
    hook = _Hook(["raise"])  # blip then idle (None) forever
    stop = threading.Event()
    game_gone = threading.Event()

    # Let it spin a couple of ticks then stop it from another thread.
    threading.Timer(0.05, stop.set).start()
    serve(mem, [("dialogue", hook, lambda j: j)], stop=stop, game_gone=game_gone)

    assert not game_gone.is_set()
    # stop was set by the timer, not by serve — proving the blip alone didn't break the loop.
    assert hook.calls >= 1


# =============================================================================================== #
# run(): supervisory re-attach loop                                                               #
# =============================================================================================== #


class _FakeSpec:
    """A minimal HookSpec-like object — only the attributes _build_fn / install branch on."""

    name = "dialogue"
    player = False
    return_hook = False
    is_name = False
    reward_field_indices = ()
    wrap_width = 46
    lines_per_page = 3
    sync = True


class _FakeFound:
    def __init__(self) -> None:
        self.spec = _FakeSpec()
        self.func_addr = 0x400000


class _FakeHookObj:
    """The installed-hook surface hook_session + serve touch: func_addr/saved_bytes/restore."""

    def __init__(self) -> None:
        self.func_addr = 0x400000
        self.saved_bytes = b"\x90" * 5

    def restore(self, mem) -> None:
        pass


class _FakeTranslator:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

        class _Cache:
            def __init__(self) -> None:
                self.closed = False

            def close(self):
                self.closed = True

        self.cache = _Cache()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def run_env(monkeypatch):
    """Stub every PID-independent + per-attach dependency of run() so the loop is hermetic.

    Records how many times hooks were installed and how many serve() calls happened, and lets each
    test script serve()'s outcome (set game_gone / raise KeyboardInterrupt / return) per call.
    """
    state = {
        "patch_calls": [],
        "installs": 0,
        "sessions": 0,
        "serves": 0,
        "serve_script": [],
        "waits": [],
        "first_pid": 100,
        "reattach_pid": 200,
    }

    # Config with auto_apply on by default; tests flip it as needed.
    cfg = cfg_mod.Config()
    cfg.patch.auto_apply = True
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg)

    # Staleness-gated auto-refresh (#19): keep the harness hermetic — default to a FRESH DB so the
    # auto-sync never fires (no network), and record any _run_sync call for the gating tests. Tests
    # that exercise the gate flip is_db_stale / inspect state["sync_calls"].
    state["sync_calls"] = 0
    import dqxclarity.translate.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "is_db_stale", lambda max_age: False)
    monkeypatch.setattr(freshness_mod, "db_age_days", lambda: 0.0)

    def fake_run_sync(c):
        state["sync_calls"] += 1
        return True

    monkeypatch.setattr(cli, "_run_sync", fake_run_sync)

    # PID-INDEPENDENT objects (module-level names on cli).
    translator = _FakeTranslator()
    monkeypatch.setattr(cli, "_build_translator", lambda c: translator)
    monkeypatch.setattr(cli, "_suppressions_path", lambda: Path("/dev/null"))
    monkeypatch.setattr(cli, "_reward_items_path", lambda: Path("/dev/null"))
    monkeypatch.setattr(cli, "_apply_patches_for_run",
                        lambda c: state["patch_calls"].append(1))

    # Names run() imports LOCALLY must be patched on their SOURCE modules, not on cli.
    import dqxclarity.translate.community as community_mod
    monkeypatch.setattr(community_mod, "load_suppressions_local", lambda p: {"x": "y"})
    monkeypatch.setattr(community_mod, "load_reward_items_local", lambda p: {"a": "b"})

    import dqxclarity.translate.suppression as supp_mod
    monkeypatch.setattr(supp_mod, "SuppressionIndex", lambda s: object())

    import dqxclarity.runtime.dispatch as dispatch_mod

    def fake_serve(mem, installed, *, stop, game_gone=None, on_line=None):
        state["serves"] += 1
        action = state["serve_script"].pop(0)
        return action(stop, game_gone)

    monkeypatch.setattr(dispatch_mod, "serve", fake_serve)

    # Name scanner: stub start_scanner so the supervisory-loop tests stay hermetic (no real thread,
    # no real pattern_scan against the fake mem dict). Record start/stop calls + enabled flag so a
    # dedicated test can assert the per-attach lifecycle. The real start_scanner/run logic is covered
    # directly in test_run_names.py. Patch on the SOURCE module (run imports it locally).
    import dqxclarity.runtime.names_loop as names_loop_mod
    state["scanner_starts"] = []
    state["scanner_stops"] = 0

    class _FakeScanner:
        def __init__(self, enabled):
            self.enabled = enabled

        def stop_and_join(self, timeout=None):
            state["scanner_stops"] += 1

    def fake_start_scanner(mem, translator, *, enabled, interval=1.0, on_write=None):
        state["scanner_starts"].append({"mem": mem, "enabled": enabled, "interval": interval})
        return _FakeScanner(enabled)

    monkeypatch.setattr(names_loop_mod, "start_scanner", fake_start_scanner)

    # Notice scanner: stub start_notice_scanner the same way (the startup "Important Notice" is a
    # static memory buffer with no hook, scanned in its own per-attach daemon thread). Record start/
    # stop calls + enabled flag so a dedicated test can assert the per-attach lifecycle. The real
    # start_notice_scanner/run logic is covered directly in test_notice.py.
    import dqxclarity.runtime.notice_loop as notice_loop_mod
    state["notice_starts"] = []
    state["notice_stops"] = 0

    class _FakeNoticeScanner:
        def __init__(self, enabled):
            self.enabled = enabled

        def stop_and_join(self, timeout=None):
            state["notice_stops"] += 1

    def fake_start_notice_scanner(mem, translate_fn, *, enabled, interval=1.0, on_write=None):
        state["notice_starts"].append({"mem": mem, "enabled": enabled, "interval": interval})
        return _FakeNoticeScanner(enabled)

    monkeypatch.setattr(notice_loop_mod, "start_notice_scanner", fake_start_notice_scanner)
    # _build_fn calls build_translate_fn for a plain dialogue spec; stub it so the loop test stays
    # decoupled from translator internals (fn building itself is covered by test_translate.py).
    monkeypatch.setattr(dispatch_mod, "build_translate_fn",
                        lambda *a, **k: (lambda ja: ja, None))

    import dqxclarity.process.memory_linux as mem_mod
    monkeypatch.setattr(mem_mod, "LinuxProcessMemory", lambda pid: {"pid": pid})
    monkeypatch.setattr(cli.hookjournal, "recover_orphans", lambda mem, pid: [])

    import dqxclarity.process.hooks as hookmod
    monkeypatch.setattr(hookmod, "locate", lambda mem, names: [_FakeFound()])

    def fake_install(mem, found):
        state["installs"] += 1
        return _FakeHookObj()

    monkeypatch.setattr(hookmod, "install", fake_install)

    # hook_session: yield a real Event so serve()/duration can drive it; restore is a no-op here.
    from contextlib import contextmanager

    @contextmanager
    def fake_session(mem, pid, hooks, *, console):
        state["sessions"] += 1
        stop = threading.Event()
        stop.signaled = False  # parity with the real hook_session (which always initializes this)
        yield stop

    monkeypatch.setattr(cli.hookjournal, "hook_session", fake_session)

    # find_game_pid / _wait_for_game: scripted pids.
    monkeypatch.setattr(cli, "find_game_pid", lambda: state["first_pid"])

    def fake_wait(poll=1.5):
        state["waits"].append(1)
        return state["reattach_pid"]

    monkeypatch.setattr(cli, "_wait_for_game", fake_wait)

    return {"cfg": cfg, "translator": translator, "state": state}


def _gone(stop, game_gone):
    """serve() outcome: the game vanished — set game_gone + stop, return 0 served."""
    if game_gone is not None:
        game_gone.set()
    stop.set()
    return 0


def _user_quit(stop, game_gone):
    """serve() outcome: the user pressed Ctrl-C."""
    raise KeyboardInterrupt


def _duration_stop(stop, game_gone):
    """serve() outcome: stop flipped (e.g. by the duration Timer) WITHOUT game_gone -> exit."""
    stop.set()
    return 7


def _gone_with_signal(stop, game_gone):
    """serve() outcome: the game vanished AND a SIGTERM/SIGHUP was handled in the same session.

    hook_session's signal handler sets ``stop.signaled``; we simulate that here alongside game_gone
    so the run() decision must EXIT (honour the signal) rather than re-attach on the game-gone.
    """
    if game_gone is not None:
        game_gone.set()
    stop.signaled = True
    stop.set()
    return 0


def test_run_reattaches_on_game_gone_then_exits_on_user_quit(run_env, capsys):
    """(a) serve returns with game_gone -> re-attach (2nd install); next serve is a user quit -> exit."""
    st = run_env["state"]
    st["serve_script"] = [_gone, _user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert st["installs"] == 2     # attached, game went away, RE-ATTACHED
    assert st["serves"] == 2       # one serve per attach
    assert st["sessions"] == 2     # hook_session wrapped BOTH sessions (orphan-safety per attach)
    assert run_env["translator"].stopped is True       # torn down ONCE after the loop
    assert run_env["translator"].cache.closed is True
    out = capsys.readouterr().out
    assert "game closed" in out    # the re-attach notice printed


def test_run_sigterm_during_game_gone_exits_not_reattach(run_env, capsys):
    """A SIGTERM that lands in the same tick the game vanished EXITS (no infinite re-attach loop)."""
    st = run_env["state"]
    # If the loop ignored stop.signaled it would re-attach forever; a single game-gone-with-signal
    # must terminate after one attach.
    st["serve_script"] = [_gone_with_signal]

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert st["installs"] == 1   # attached once; the signal exited instead of re-attaching
    assert st["serves"] == 1
    assert st["sessions"] == 1
    assert run_env["translator"].stopped is True
    out = capsys.readouterr().out
    assert "game closed" not in out   # did NOT print the re-attach notice


def test_run_keyboardinterrupt_exits_without_reattaching(run_env):
    """(b) serve returns via KeyboardInterrupt -> exit immediately, no re-attach."""
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert st["installs"] == 1   # attached exactly once; NO re-attach on a user quit
    assert st["serves"] == 1
    assert run_env["translator"].stopped is True


def test_run_patches_only_on_first_attach(run_env):
    """(c) the patch step runs on the FIRST iteration only, never on a re-attach."""
    st = run_env["state"]
    # game-gone twice then a user quit -> three attaches total.
    st["serve_script"] = [_gone, _gone, _user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert st["installs"] == 3        # three attaches
    assert len(st["patch_calls"]) == 1  # but patch only on the first
    # The re-attaches went through _wait_for_game (never patched).
    assert len(st["waits"]) == 2


def test_run_no_patch_skips_patch_but_still_waits(run_env):
    """(d) --no-patch: no patch step, but the loop STILL waits for the game (no fail-fast)."""
    st = run_env["state"]
    st["first_pid"] = None  # game down at the top
    st["reattach_pid"] = 200
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=False)

    assert st["patch_calls"] == []   # patch step skipped
    assert len(st["waits"]) == 1     # but we waited for the game on the first attach
    assert st["installs"] == 1


def test_run_duration_stop_ends_the_whole_service_not_a_reattach(run_env):
    """A duration/SIGTERM stop (no game_gone) ends the supervisory loop, never re-attaches."""
    st = run_env["state"]
    st["serve_script"] = [_duration_stop]

    cli.run(hooks="dialogue", duration=5.0, patch=True)

    assert st["installs"] == 1   # exactly one attach; the stop-without-game_gone exits the loop
    assert run_env["translator"].stopped is True


def test_run_no_hooks_installed_exits_and_tears_down(run_env, monkeypatch):
    """No hooks resolve -> Exit(1), and the translator/cache are still torn down (via finally)."""
    import dqxclarity.process.hooks as hookmod
    monkeypatch.setattr(hookmod, "locate", lambda mem, names: [])  # nothing resolves
    monkeypatch.setattr(cli, "HOOK_LOCATE_RETRY_SECS", 0.0)  # skip the early-attach locate retry (#31)

    with pytest.raises(typer.Exit) as ei:
        cli.run(hooks="dialogue", duration=0.0, patch=True)
    assert ei.value.exit_code == 1
    assert run_env["translator"].stopped is True   # finally tore it down even on the early exit
    assert run_env["translator"].cache.closed is True


# =============================================================================================== #
# run(): staleness-gated auto-refresh (#19)                                                       #
# =============================================================================================== #


def test_run_stale_db_triggers_one_auto_sync(run_env, monkeypatch):
    """A STALE (or never-synced) DB triggers exactly ONE _run_sync before serving begins."""
    import dqxclarity.translate.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "is_db_stale", lambda max_age: True)
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, sync=True)

    assert st["sync_calls"] == 1   # the stale DB was refreshed once


def test_run_fresh_db_does_not_sync(run_env):
    """A FRESH DB (the fixture default) performs NO auto-sync — zero network at startup."""
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, sync=True)

    assert st["sync_calls"] == 0   # fresh -> the local check short-circuits, no _run_sync


def test_run_no_sync_flag_skips_even_when_stale(run_env, monkeypatch):
    """--no-sync skips the refresh even when the DB is stale (and does no local-vs-network work)."""
    import dqxclarity.translate.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "is_db_stale", lambda max_age: True)
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, sync=False)

    assert st["sync_calls"] == 0


def test_run_auto_sync_disabled_skips_even_when_stale(run_env, monkeypatch):
    """translate.auto_sync=False skips the refresh even when stale (config opt-out honoured)."""
    import dqxclarity.translate.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "is_db_stale", lambda max_age: True)
    run_env["cfg"].translate.auto_sync = False
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, sync=True)

    assert st["sync_calls"] == 0


def test_run_auto_sync_failure_does_not_abort_run(run_env, monkeypatch):
    """A network failure during the auto-refresh prints a warning and run() carries on serving."""
    import httpx

    import dqxclarity.translate.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "is_db_stale", lambda max_age: True)

    def boom(c):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(cli, "_run_sync", boom)
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, sync=True)

    # The run still attached + served despite the failed refresh (no abort).
    assert st["installs"] == 1
    assert run_env["translator"].stopped is True


# =============================================================================================== #
# files.py: backup-set pruning                                                                    #
# =============================================================================================== #


def _make_backup_set(backup_dir: Path, name: str, *, mtime: float) -> Path:
    """Create a backup-set dir with a backup.json and a controlled mtime."""
    d = backup_dir / name
    d.mkdir(parents=True)
    (d / "backup.json").write_text("{}", encoding="utf-8")
    import os
    os.utime(d, (mtime, mtime))
    return d


def test_prune_keeps_newest_n_and_deletes_older_for_manifest(tmp_path):
    """Pruning keeps the newest N sets for the manifest and deletes the rest."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    base = time.time()
    # 13 sets for manifest "test" with increasing mtime (older first).
    dirs = []
    for i in range(13):
        dirs.append(_make_backup_set(backup_dir, f"test-2026010{i:02d}", mtime=base + i))

    pruned = patch_files._prune_backup_sets(backup_dir, "test", keep=10)

    # Newest 10 survive; the 3 oldest are pruned.
    surviving = sorted(d.name for d in backup_dir.iterdir() if d.is_dir())
    assert len(surviving) == 10
    assert {p.name for p in pruned} == {d.name for d in dirs[:3]}  # the 3 oldest
    for d in dirs[:3]:
        assert not d.exists()
    for d in dirs[3:]:
        assert d.exists()


def test_prune_never_touches_unrelated_dirs(tmp_path):
    """Only THIS manifest's backup sets are eligible; other manifests + stray dirs are untouched."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    base = time.time()
    # 12 sets for "test" (so 2 should be pruned at keep=10).
    test_dirs = [_make_backup_set(backup_dir, f"test-stamp{i:02d}", mtime=base + i) for i in range(12)]
    # A different manifest's sets (many, all old) — must NEVER be pruned by a "test" prune.
    other_dirs = [_make_backup_set(backup_dir, f"OtherManifest-s{i}", mtime=base - 100 + i)
                  for i in range(5)]
    # A stray dir with no backup.json and a stray file — both must survive untouched.
    stray = backup_dir / "test-not-a-backup-set"
    stray.mkdir()
    (backup_dir / "loose.txt").write_text("x")

    pruned = patch_files._prune_backup_sets(backup_dir, "test", keep=10)

    assert len(pruned) == 2  # only "test" sets pruned, only 2 of them
    # All five other-manifest sets survive.
    for d in other_dirs:
        assert d.exists()
    # The stray (no backup.json) and loose file survive.
    assert stray.exists()
    assert (backup_dir / "loose.txt").exists()
    # The pruned ones are the 2 oldest "test-" sets.
    assert {p.name for p in pruned} == {test_dirs[0].name, test_dirs[1].name}


def test_prune_is_noop_when_under_threshold(tmp_path):
    """Fewer than N sets -> nothing pruned."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    base = time.time()
    for i in range(5):
        _make_backup_set(backup_dir, f"test-{i}", mtime=base + i)
    assert patch_files._prune_backup_sets(backup_dir, "test", keep=10) == []
    assert len(list(backup_dir.iterdir())) == 5


def test_prune_handles_manifest_name_with_spaces(tmp_path):
    """The prefix matches apply()'s space->underscore naming for the manifest."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    base = time.time()
    dirs = [_make_backup_set(backup_dir, f"DQX_Clarity-{i:02d}", mtime=base + i) for i in range(12)]
    pruned = patch_files._prune_backup_sets(backup_dir, "DQX Clarity", keep=10)
    assert len(pruned) == 2
    assert {p.name for p in pruned} == {dirs[0].name, dirs[1].name}


def test_apply_prunes_after_real_install(tmp_path, monkeypatch):
    """End-to-end: a real apply that creates a backup set prunes older sets for the manifest."""
    from dqxclarity.patching.manifest import Manifest
    from dqxclarity.process.discover import GameInstall

    root = tmp_path / "DRAGON QUEST X"
    (root / "Game" / "Content" / "Data").mkdir(parents=True)
    (root / "Game" / "DQXGame.exe").write_bytes(b"MZ")
    target = root / "Game" / "Content" / "Data" / "data00000000.win32.idx"
    target.write_bytes(b"ORIGINAL")
    install = GameInstall(install_root=root)
    monkeypatch.setattr(patch_files, "is_game_running", lambda: False)

    manifest = Manifest.from_dict({
        "name": "test",
        "version": "1",
        "groups": {"game_files": {"files": [
            {"target": "Game/Content/Data/data00000000.win32.idx", "url": "u://idx"},
        ]}},
    })

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    base = time.time()
    # Pre-seed 12 OLD backup sets for this manifest; the apply's new set makes 13 -> prune to 10.
    old = [_make_backup_set(backup_dir, f"test-old{i:02d}", mtime=base - 1000 + i) for i in range(12)]

    monkeypatch.setattr(patch_files, "_download",
                        lambda url, dest, sha, size: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                      dest.write_bytes(b"NEW"))[0])

    summary = patch_files.apply(
        install, manifest, requested_groups={"game_files"},
        cache_dir=tmp_path / "cache", backup_dir=backup_dir, force=False, dry_run=False,
    )

    assert summary["backup_set"] is not None
    sets = [d for d in backup_dir.iterdir() if d.is_dir() and (d / "backup.json").is_file()]
    assert len(sets) == 10  # 13 (12 old + 1 new) pruned down to the newest 10
    # The new set (most recent) survives; the oldest pre-seeded ones are gone.
    assert Path(summary["backup_set"]).exists()
    assert not old[0].exists()
