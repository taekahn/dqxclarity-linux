"""Tests for wiring the polling NAME scanner into `dqxclarity run` (backlog #30).

The scanner itself (names_loop.run) and its memory backend are covered elsewhere; here we cover the
WIRING added for #30:

  1. names_loop.start_scanner — the per-attach helper that makes the scanner thread testable:
       * enabled  -> starts a daemon thread that calls names_loop.run with the given mem + translator
                     and a private stop Event; stop_and_join() sets that event and joins the thread.
       * disabled -> NO thread starts; stop_and_join() is a safe no-op.
  2. run()'s --names flag exists, defaults True, and drives start_scanner's `enabled` per attach;
     --no-names disables it. The scanner is stopped+joined once per attach (before any re-attach),
     reusing the ONE translator built before the supervisory loop.

The run()-level checks reuse the hermetic `run_env` fixture from test_lifecycle.py (which stubs
start_scanner with a recorder), so no real thread or game is touched.
"""

from __future__ import annotations

import threading
import time

from dqxclarity import cli
from dqxclarity.runtime import names_loop

# Reuse the fully-stubbed run() harness + serve outcome helpers from the lifecycle suite. pytest's
# default (prepend) import mode puts the tests dir on sys.path, so the bare module name resolves.
from test_lifecycle import _user_quit, run_env  # noqa: F401  (run_env is a fixture)


# =============================================================================================== #
# names_loop.start_scanner — the testable per-attach helper                                       #
# =============================================================================================== #


class _RecordingTranslator:
    """Stand-in translator: start_scanner only passes it through to names_loop.run unchanged."""


def test_start_scanner_enabled_starts_thread_with_mem_and_translator(monkeypatch):
    """enabled -> a thread starts that calls names_loop.run with the SAME mem+translator+a stop."""
    seen = {}
    started = threading.Event()

    def fake_run(mem, translator, *, stop, interval, on_write):
        seen["mem"] = mem
        seen["translator"] = translator
        seen["interval"] = interval
        seen["on_write"] = on_write
        seen["stop"] = stop
        started.set()
        stop.wait()  # block like the real loop until stop is set

    monkeypatch.setattr(names_loop, "run", fake_run)

    mem = object()
    translator = _RecordingTranslator()
    cb = lambda ja, en: None  # noqa: E731
    handle = names_loop.start_scanner(mem, translator, enabled=True, interval=0.25, on_write=cb)

    assert started.wait(2.0), "the scanner thread never invoked names_loop.run"
    assert handle.thread is not None and handle.thread.is_alive()
    assert handle.thread.daemon is True  # must never block process exit
    assert seen["mem"] is mem
    assert seen["translator"] is translator
    assert seen["interval"] == 0.25
    assert seen["on_write"] is cb
    # The thread got start_scanner's private stop Event (NOT the supervisor's shared stop).
    assert seen["stop"] is handle.stop
    assert not handle.stop.is_set()

    handle.stop_and_join(timeout=2.0)
    assert handle.stop.is_set()
    assert not handle.thread.is_alive()


def test_start_scanner_disabled_starts_no_thread(monkeypatch):
    """disabled -> no thread at all; names_loop.run is never invoked; stop_and_join is a no-op."""
    called = []
    monkeypatch.setattr(names_loop, "run", lambda *a, **k: called.append(1))

    handle = names_loop.start_scanner(object(), object(), enabled=False, interval=1.0)

    assert handle.thread is None
    # Give any (erroneously) spawned thread a moment; there should be none.
    time.sleep(0.05)
    assert called == []
    # Safe no-op: sets its (unused) stop and returns without trying to join a None thread.
    handle.stop_and_join(timeout=1.0)
    assert handle.stop.is_set()


def test_stop_and_join_sets_event_and_joins(monkeypatch):
    """stop_and_join sets the private event (unblocking names_loop.run) and joins the thread."""
    ran_to_completion = threading.Event()

    def fake_run(mem, translator, *, stop, interval, on_write):
        stop.wait()             # the real loop blocks until stop is set
        ran_to_completion.set()  # reached only once stop is set -> proves join saw a clean exit

    monkeypatch.setattr(names_loop, "run", fake_run)

    handle = names_loop.start_scanner(object(), object(), enabled=True, interval=1.0)
    assert handle.thread.is_alive()

    handle.stop_and_join(timeout=2.0)

    assert handle.stop.is_set()
    assert ran_to_completion.is_set()
    assert not handle.thread.is_alive()


# =============================================================================================== #
# run(): the --names flag drives the per-attach scanner                                           #
# =============================================================================================== #


def test_run_flag_names_defaults_true_and_starts_scanner(run_env):  # noqa: F811
    """The --names flag defaults True: a normal `run` starts the scanner (enabled) for the attach."""
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    # Don't pass names -> exercise the typer default. (Direct call leaves the OptionInfo default,
    # which run() coerces via bool(names); the wiring must treat the default as ON.)
    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert len(st["scanner_starts"]) == 1            # one scanner per attach
    assert st["scanner_starts"][0]["enabled"] is True  # default is ON
    # Started against THIS attach's mem (the fake LinuxProcessMemory dict) and the ONE translator.
    assert st["scanner_starts"][0]["mem"] == {"pid": 100}
    # The interval default must thread through as a concrete float, not typer's OptionInfo sentinel
    # (run() coerces with float(names_interval)); an un-coerced sentinel would crash stop.wait(interval).
    assert st["scanner_starts"][0]["interval"] == 1.0
    assert isinstance(st["scanner_starts"][0]["interval"], float)
    assert st["scanner_stops"] == 1                  # stopped+joined once for the attach


def test_run_no_names_disables_scanner(run_env):  # noqa: F811
    """--no-names -> start_scanner is still called (uniform call site) but with enabled=False."""
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, names=False)

    assert len(st["scanner_starts"]) == 1
    assert st["scanner_starts"][0]["enabled"] is False  # disabled
    assert st["scanner_stops"] == 1                      # still stop_and_join'd (no-op handle)


def test_run_scanner_started_and_stopped_per_attach(run_env):  # noqa: F811
    """A game-gone re-attach starts a FRESH scanner bound to the new mem, and each is stopped once."""
    from test_lifecycle import _gone

    st = run_env["state"]
    st["serve_script"] = [_gone, _user_quit]  # attach, game-gone, re-attach, user quit

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert len(st["scanner_starts"]) == 2   # one scanner per attach
    # First attach uses the first pid's mem, the re-attach uses the new pid's mem.
    assert st["scanner_starts"][0]["mem"] == {"pid": 100}
    assert st["scanner_starts"][1]["mem"] == {"pid": 200}
    assert st["scanner_stops"] == 2         # each attach's scanner stopped+joined exactly once


def test_run_uses_names_interval(run_env):  # noqa: F811
    """--names-interval is threaded through to start_scanner's interval."""
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, names=True, names_interval=2.5)

    assert st["scanner_starts"][0]["interval"] == 2.5


# =============================================================================================== #
# names_loop.run — player/sibling collision (the "Squid" bug)                                     #
# =============================================================================================== #


class _OneShotStop:
    """A stop Event stand-in that lets names_loop.run execute EXACTLY one scan iteration."""

    def __init__(self) -> None:
        self._n = 0

    def is_set(self) -> bool:
        self._n += 1
        return self._n > 1  # False on the loop's first check, True thereafter

    def wait(self, _timeout) -> None:
        pass


class _CollisionMem:
    """Minimal mem: every name-pattern scan yields one match whose name is the player's JA name."""

    def __init__(self, ja: str) -> None:
        self._ja = ja
        self.writes: list[str] = []

    def pattern_scan(self, pattern, *, data_only=True, limit=200):
        return [0x1000]

    def read_cstring(self, addr, n=64):
        return self._ja

    def write_cstring(self, addr, text, *, max_bytes):
        self.writes.append(text)
        return True


class _CollisionTranslator:
    """player タイカン collides with a cached monster タイカン -> 'Squid'; the pin must win."""

    def __init__(self) -> None:
        self.player_name_ja = "タイカン"
        self.player_name_en = "Taikan"
        self.sibling_name_ja = "きみこ"
        self.sibling_name_en = "Kimiko"

    def translate_name(self, ja: str) -> str:
        return "Squid" if ja == "タイカン" else "romaji(" + ja + ")"


def test_scanner_player_name_beats_cache_collision():
    # The player's own name in the party buffer must resolve to the pinned EN name, NOT the cached
    # monster collision. Every write the scanner makes for タイカン must be 'Taikan', never 'Squid'.
    mem = _CollisionMem("タイカン")
    names_loop.run(mem, _CollisionTranslator(), stop=_OneShotStop(), interval=0)
    assert mem.writes, "scanner should have written at least once"
    assert all("Taikan" in w for w in mem.writes)
    assert all("Squid" not in w for w in mem.writes)


def test_scanner_sibling_name_beats_cache_collision():
    mem = _CollisionMem("きみこ")
    names_loop.run(mem, _CollisionTranslator(), stop=_OneShotStop(), interval=0)
    assert mem.writes
    assert all("Kimiko" in w for w in mem.writes)


def test_scanner_non_player_name_uses_translate_name():
    # A normal monster/NPC name (no player/sibling collision) still goes through translate_name.
    mem = _CollisionMem("スライム")
    names_loop.run(mem, _CollisionTranslator(), stop=_OneShotStop(), interval=0)
    assert mem.writes
    assert all("romaji(スライム)" in w for w in mem.writes)
