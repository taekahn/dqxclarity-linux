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
from dqxclarity.process.memory_linux import MapRegion
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

    def fake_run(mem, translator, *, stop, interval, on_write, profiler=None):
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

    def fake_run(mem, translator, *, stop, interval, on_write, profiler=None):
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
    """Minimal mem: every name-pattern scan yields one match whose name is the player's JA name.

    The hit at 0x1000 lives inside the single fake region returned by ``scannable_regions`` so the
    warm-region bookkeeping in names_loop.run can map it back to a region (otherwise the warm set
    would never populate and behavior would differ from the real loop).
    """

    def __init__(self, ja: str) -> None:
        self._ja = ja
        self.writes: list[str] = []

    def scannable_regions(self, *, data_only=True):
        return [MapRegion(0x1000, 0x2000, "rw-p", "[heap]")]

    def pattern_scan(self, pattern, *, data_only=True, limit=200, regions=None, _chunk=None):
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


# =============================================================================================== #
# names_loop.run — warm-region discovery/maintenance split (the lag fix)                          #
# =============================================================================================== #


class _NTickStop:
    """A stop Event stand-in that lets names_loop.run execute EXACTLY ``n`` tick iterations."""

    def __init__(self, n: int) -> None:
        self._n = 0
        self._limit = n

    def is_set(self) -> bool:
        self._n += 1
        return self._n > self._limit  # False for the first ``n`` checks, True after

    def wait(self, _timeout) -> None:
        pass


# Three distinct fake data regions. "warm" is where names live; the others are the rest of the heap
# that a full sweep also pays for but which never contain names.
_R_WARM = MapRegion(0x10000, 0x11000, "rw-p", "[heap]")
_R_COLD1 = MapRegion(0x20000, 0x21000, "rw-p", "[anon]")
_R_COLD2 = MapRegion(0x30000, 0x31000, "rw-p", "[anon]")
_R_NEW = MapRegion(0x40000, 0x41000, "rw-p", "[heap]")  # where a name appears after a "zone change"


class _PlainTranslator:
    """No player/sibling pins; translate_name just romanizes so JA != EN (so a write happens)."""

    player_name_ja = None
    player_name_en = None
    sibling_name_ja = None
    sibling_name_en = None

    def translate_name(self, ja: str) -> str:
        return "romaji(" + ja + ")"


class _RegionMem:
    """Fake mem that serves a single JA name living at a configurable address inside a region set.

    ``pattern_scan`` records the region list it was asked to scan (so tests can prove which regions
    were swept), and returns the hit address only when the hit's owning region is in that list — so
    a warm-only pass that doesn't include the name's region correctly comes up empty (zone change).
    """

    def __init__(self, regions: list[MapRegion], hit_addr: int | None, ja: str) -> None:
        self._regions = regions
        self._hit_addr = hit_addr  # absolute name match address, or None for "no names anywhere"
        self._ja = ja
        self.scan_calls: list[list[MapRegion]] = []  # one entry per pattern_scan call
        self.writes: list[str] = []

    def set_hit(self, hit_addr: int | None) -> None:
        self._hit_addr = hit_addr

    def scannable_regions(self, *, data_only=True):
        return list(self._regions)

    def pattern_scan(self, pattern, *, data_only=True, limit=200, regions=None, _chunk=None):
        scanned = regions if regions is not None else self._regions
        self.scan_calls.append(list(scanned))
        if self._hit_addr is None:
            return []
        # Return the hit only if its region is among those being scanned this pass.
        for r in scanned:
            if r.start <= self._hit_addr < r.end:
                return [self._hit_addr]
        return []

    def read_cstring(self, addr, n=64):
        return self._ja

    def write_cstring(self, addr, text, *, max_bytes):
        self.writes.append(text)
        return True


# The name-match offset back to the JA string varies per NamePattern; place the hit at the region
# start and read the JA name regardless of addr (the fake ignores addr), so name_offset is moot.
def _full_region_calls(mem):
    """Region lists from scan calls that swept the FULL region set (all three cold+warm regions)."""
    full = {_R_WARM.start, _R_COLD1.start, _R_COLD2.start}
    return [c for c in mem.scan_calls if {r.start for r in c} == full]


def _warm_only_calls(mem):
    """Region lists from scan calls that swept ONLY the warm region."""
    return [c for c in mem.scan_calls if {r.start for r in c} == {_R_WARM.start}]


def test_first_tick_full_scan_then_warm_only(monkeypatch):
    """Tick 1 sweeps ALL regions and populates the warm set; tick 2 scans ONLY the warm region."""
    monkeypatch.setattr(names_loop, "FULL_RESCAN_SECS", 1000.0)  # don't let the periodic timer fire
    mem = _RegionMem([_R_WARM, _R_COLD1, _R_COLD2], _R_WARM.start, "スライム")

    stats = names_loop.run(mem, _PlainTranslator(), stop=_NTickStop(2), interval=0)

    # Exactly one full sweep (tick 1) and one warm-only tick (tick 2).
    assert stats.full_scans == 1
    assert stats.warm_scans == 1
    # Tick 1 swept the full set for every pattern; tick 2 swept only the warm region.
    assert _full_region_calls(mem), "first tick must do a full sweep"
    assert _warm_only_calls(mem), "second tick must scan only the warm region"
    # And tick 2 must NOT have re-swept the full set.
    n_patterns = len(names_loop.NAME_PATTERNS)
    assert len(_full_region_calls(mem)) == n_patterns       # full sweep happened once (tick 1)
    assert len(_warm_only_calls(mem)) == n_patterns         # warm-only happened once (tick 2)
    assert mem.writes  # the name was translated+written


def test_periodic_full_rescan_after_timer(monkeypatch):
    """After FULL_RESCAN_SECS elapses, a full re-sweep happens even with a non-empty warm set."""
    monkeypatch.setattr(names_loop, "FULL_RESCAN_SECS", 5.0)
    # Drive a controllable clock so the timer fires deterministically between ticks.
    clock = {"t": 1000.0}
    monkeypatch.setattr(names_loop.time, "monotonic", lambda: clock["t"])
    mem = _RegionMem([_R_WARM, _R_COLD1, _R_COLD2], _R_WARM.start, "スライム")

    # Tick 1: full (warm empty). Tick 2: warm-only (timer not elapsed). Advance past the timer.
    class _ClockStop:
        def __init__(self):
            self._n = 0

        def is_set(self):
            self._n += 1
            return self._n > 3  # three ticks

        def wait(self, _t):
            clock["t"] += 3.0  # each interval advances the clock 3s -> exceeds 5s by tick 3

    names_loop.run(mem, _PlainTranslator(), stop=_ClockStop(), interval=0)

    # Two full sweeps: tick 1 (initial) and tick 3 (periodic timer elapsed at t=1006 >= 1000+5).
    assert len(_full_region_calls(mem)) == 2 * len(names_loop.NAME_PATTERNS)


def test_zone_change_triggers_rediscovery(monkeypatch):
    """Warm region goes empty (zone change) -> a full rediscovery sweep finds the name's new home."""
    monkeypatch.setattr(names_loop, "FULL_RESCAN_SECS", 1000.0)  # isolate from the periodic timer
    # Region set includes both the old warm region and the NEW region the name moves to.
    mem = _RegionMem([_R_WARM, _R_COLD1, _R_NEW], _R_WARM.start, "スライム")

    # Tick 1: full sweep, name found in _R_WARM -> warm = {_R_WARM}.
    # Tick 2: warm-only scan of _R_WARM. We move the name to _R_NEW BEFORE tick 2 so the warm scan
    #         returns zero -> warm cleared.
    # Tick 3: warm empty + backoff not armed -> full rediscovery sweep, finds the name in _R_NEW.
    class _MovingStop:
        def __init__(self):
            self._n = 0

        def is_set(self):
            self._n += 1
            if self._n == 2:
                mem.set_hit(_R_NEW.start)  # the buffers moved between tick 1 and tick 2
            return self._n > 3

        def wait(self, _t):
            pass

    stats = names_loop.run(mem, _PlainTranslator(), stop=_MovingStop(), interval=0)

    # Two full sweeps: the initial discovery and the post-zone-change rediscovery.
    assert stats.full_scans == 2
    # Exactly one warm-only tick (tick 2) — the one that came up empty and triggered rediscovery.
    assert stats.warm_scans == 1
    # The rediscovery sweep found the name in its new region and wrote it.
    assert mem.writes, "rediscovery must find and write the moved name"


def test_no_names_backs_off_no_repeated_full_sweeps(monkeypatch):
    """A full sweep that finds nothing must NOT full-sweep again every tick (backoff)."""
    monkeypatch.setattr(names_loop, "FULL_RESCAN_SECS", 1000.0)  # long backoff so it stays armed
    clock = {"t": 5000.0}
    monkeypatch.setattr(names_loop.time, "monotonic", lambda: clock["t"])
    mem = _RegionMem([_R_WARM, _R_COLD1, _R_COLD2], None, "スライム")  # no names anywhere

    class _ManyTickStop:
        def __init__(self):
            self._n = 0

        def is_set(self):
            self._n += 1
            return self._n > 10  # ten ticks

        def wait(self, _t):
            clock["t"] += 1.0  # 1s per tick; well under the 1000s backoff

    stats = names_loop.run(mem, _PlainTranslator(), stop=_ManyTickStop(), interval=0)

    # Across 10 ticks only the FIRST is a full sweep; the rest are backed off (no scan at all).
    assert stats.full_scans == 1, "must not full-sweep the heap on every tick when there are no names"
    assert len(_full_region_calls(mem)) == len(names_loop.NAME_PATTERNS)
    assert not mem.writes
