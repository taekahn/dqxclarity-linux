"""Tests for the hook journal — the unclean-exit recovery that restores orphaned detours.

The single most important property under test is the PID-SAFETY guard in ``recover_orphans``: if
the journal's recorded pid no longer matches the running game's pid, the patched process is gone
and those addresses belong to a DIFFERENT process, so recovery must write NOTHING.
"""

from __future__ import annotations

import json
import signal

import pytest

from dqxclarity.process import detour
from dqxclarity.runtime import hookjournal


class FakeMem:
    """Minimal address-space stub: a dict-backed byte store + a writes log (cf. test_serve.py)."""

    def __init__(self) -> None:
        self.buffers: dict[int, bytes] = {}
        self.writes: list[tuple[int, bytes]] = []

    def read(self, addr: int, size: int) -> bytes:
        return self.buffers.get(addr, b"")[:size]

    def write(self, addr: int, data: bytes) -> None:
        self.writes.append((addr, bytes(data)))
        self.buffers[addr] = bytes(data)


@pytest.fixture(autouse=True)
def _journal_in_tmp(tmp_path, monkeypatch):
    """Point CONFIG_DIR and JOURNAL_PATH at tmp_path so no real config dir is ever touched."""
    monkeypatch.setattr(hookjournal.config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(hookjournal, "JOURNAL_PATH", tmp_path / "active_hooks.json")
    return tmp_path


# Detour jmp opcode (0xE9) means a hook is still installed at the address.
E9 = b"\xe9"
SAVED_A = bytes.fromhex("558bec568bf1")  # a real-looking stolen prologue
SAVED_B = bytes.fromhex("8db8ec000000")


def _mem_with_detours(addr_to_saved: dict[int, bytes]) -> FakeMem:
    """A FakeMem whose hooked addresses currently hold an 0xE9 detour jmp."""
    m = FakeMem()
    for addr in addr_to_saved:
        m.buffers[addr] = E9 + b"\x00\x00\x00\x00"  # detour jmp present
    return m


def test_write_then_recover_round_trip_restores_all():
    addr_a, addr_b = 0x500000, 0x600000
    mem = _mem_with_detours({addr_a: SAVED_A, addr_b: SAVED_B})
    hookjournal.write_journal(1234, [(addr_a, SAVED_A), (addr_b, SAVED_B)])

    restored = hookjournal.recover_orphans(mem, 1234)

    assert set(restored) == {addr_a, addr_b}
    # The original prologue bytes were written back to each address.
    assert (addr_a, SAVED_A) in mem.writes
    assert (addr_b, SAVED_B) in mem.writes
    # Journal removed after a successful recovery.
    assert not hookjournal.JOURNAL_PATH.exists()


def test_pid_mismatch_writes_nothing_and_clears_journal():
    """THE critical guard: a different pid means the addresses belong to another process now."""
    addr = 0x500000
    mem = _mem_with_detours({addr: SAVED_A})  # bytes happen to look like a detour, but wrong pid
    hookjournal.write_journal(1111, [(addr, SAVED_A)])

    restored = hookjournal.recover_orphans(mem, 2222)  # pid changed -> must NOT write

    assert restored == []
    assert mem.writes == []  # NOTHING written — would have corrupted a live foreign process
    assert not hookjournal.JOURNAL_PATH.exists()  # stale journal discarded


def test_addr_already_clean_is_skipped():
    addr_clean, addr_hooked = 0x500000, 0x600000
    mem = FakeMem()
    mem.buffers[addr_clean] = b"\x55\x8b\xec\x56\x8b\xf1"  # already restored: NOT 0xE9
    mem.buffers[addr_hooked] = E9 + b"\x00\x00\x00\x00"  # still detoured
    hookjournal.write_journal(42, [(addr_clean, SAVED_A), (addr_hooked, SAVED_B)])

    restored = hookjournal.recover_orphans(mem, 42)

    assert restored == [addr_hooked]  # only the still-hooked addr is restored
    assert (addr_clean, SAVED_A) not in mem.writes  # the clean addr was NOT rewritten
    assert (addr_hooked, SAVED_B) in mem.writes
    assert not hookjournal.JOURNAL_PATH.exists()  # journal still cleared


def test_missing_journal_returns_empty_and_does_not_error():
    mem = FakeMem()
    assert not hookjournal.JOURNAL_PATH.exists()
    assert hookjournal.recover_orphans(mem, 999) == []
    assert mem.writes == []


def test_clear_journal_removes_file_and_is_noop_when_missing():
    hookjournal.write_journal(7, [(0x1000, SAVED_A)])
    assert hookjournal.JOURNAL_PATH.exists()
    hookjournal.clear_journal()
    assert not hookjournal.JOURNAL_PATH.exists()
    # No-op when already gone (must not raise).
    hookjournal.clear_journal()
    assert not hookjournal.JOURNAL_PATH.exists()


def test_write_journal_produces_valid_loadable_json():
    """Atomicity smoke: the written journal is valid JSON with the right pid + entries."""
    hookjournal.write_journal(31337, [(0xABCD, SAVED_A), (0xBEEF, SAVED_B)])
    data = json.loads(hookjournal.JOURNAL_PATH.read_text(encoding="utf-8"))
    assert data["pid"] == 31337
    assert data["hooks"] == [
        {"addr": 0xABCD, "bytes": SAVED_A.hex()},
        {"addr": 0xBEEF, "bytes": SAVED_B.hex()},
    ]
    # No leftover temp file from the atomic replace.
    assert not (hookjournal.JOURNAL_PATH.with_suffix(".json.tmp")).exists()


def test_one_bad_entry_does_not_abort_recovery():
    """A malformed entry is skipped; the good entry still gets restored."""
    good = 0x500000
    mem = _mem_with_detours({good: SAVED_A})
    # Hand-craft a journal with one entry missing its fields, then a good one.
    hookjournal.JOURNAL_PATH.write_text(
        json.dumps({"pid": 5, "hooks": [{"addr": good}, {"addr": good, "bytes": SAVED_A.hex()}]}),
        encoding="utf-8",
    )
    restored = hookjournal.recover_orphans(mem, 5)
    assert restored == [good]  # bad entry skipped, good one restored
    assert not hookjournal.JOURNAL_PATH.exists()


def test_default_spin_timeout_is_constant_and_above_floor():
    """The install defaults use SPIN_TIMEOUT, which must stay at/above the documented floor.

    Floor = 250M: the spin body is cmp/je/PAUSE/dec/jnz; PAUSE is ~10-40 cycles, so on a 3+ GHz CPU
    at ~10 cycles/iter, ~300M ≈ ~1.0s — just enough to cover a worst-case ~1s synchronous Google MT
    call so a legitimate first-view translation isn't cut off and shown as Japanese. Below ~250M
    (~0.5s on fast hardware) that worst case is no longer covered → a translation regression. The
    earlier 100M figure was wrong; the real floor for covering ~1s of sync MT is ~250-300M.
    """
    import inspect

    assert detour.SPIN_TIMEOUT >= 250_000_000
    bsig = inspect.signature(detour.install_blocking_hook)
    rsig = inspect.signature(detour.install_return_hook)
    assert bsig.parameters["timeout"].default == detour.SPIN_TIMEOUT
    assert rsig.parameters["timeout"].default == detour.SPIN_TIMEOUT
    # SPIN_TIMEOUT is the single source of truth: the shellcode builders default to it too, so a
    # direct caller can't get a stale/too-low value.
    bbuild = inspect.signature(detour.build_blocking_shellcode)
    rbuild = inspect.signature(detour.build_return_shellcode)
    assert bbuild.parameters["timeout"].default == detour.SPIN_TIMEOUT
    assert rbuild.parameters["timeout"].default == detour.SPIN_TIMEOUT


def test_pid_mismatch_float_lookalike_writes_nothing():
    """Strict pid type guard: a hand-edited FLOAT pid that == the int pid must NOT pass.

    Python's `1234.0 == 1234` is True, so without the isinstance check a float pid would sneak past
    the equality test and let recovery write into a process whose pid we never actually verified.
    """
    addr = 0x500000
    mem = _mem_with_detours({addr: SAVED_A})
    # Hand-craft a journal whose pid is a FLOAT that equals the running game pid.
    hookjournal.JOURNAL_PATH.write_text(
        json.dumps({"pid": 1234.0, "hooks": [{"addr": addr, "bytes": SAVED_A.hex()}]}),
        encoding="utf-8",
    )
    restored = hookjournal.recover_orphans(mem, 1234)
    assert restored == []
    assert mem.writes == []  # NOTHING written — a float pid is not a verified match
    assert not hookjournal.JOURNAL_PATH.exists()  # stale/untrusted journal discarded


def test_recover_corrupt_unparseable_journal_writes_nothing_and_clears():
    """A corrupt/unparseable journal: write nothing, return [], and discard the file."""
    mem = FakeMem()
    hookjournal.JOURNAL_PATH.write_bytes(b"{bad")  # not valid JSON

    restored = hookjournal.recover_orphans(mem, 1234)

    assert restored == []
    assert mem.writes == []
    assert not hookjournal.JOURNAL_PATH.exists()  # discarded, never trusted


def test_recover_unreadable_addr_does_not_abort_recovery():
    """An OSError reading one addr must not abort recovery: the other (good) addr still restores."""
    addr_bad, addr_good = 0x500000, 0x600000

    class RaisingMem(FakeMem):
        def read(self, addr, size):
            if addr == addr_bad:
                raise OSError("unreadable page")
            return super().read(addr, size)

    mem = RaisingMem()
    mem.buffers[addr_good] = E9 + b"\x00\x00\x00\x00"  # still detoured
    hookjournal.write_journal(1234, [(addr_bad, SAVED_A), (addr_good, SAVED_B)])

    restored = hookjournal.recover_orphans(mem, 1234)

    assert restored == [addr_good]  # only the readable one restored; bad addr skipped, not fatal
    assert (addr_good, SAVED_B) in mem.writes
    assert all(a != addr_bad for a, _ in mem.writes)  # nothing written at the unreadable addr
    assert not hookjournal.JOURNAL_PATH.exists()


# ---- hook_session context manager (run's hook lifecycle) ------------------------------------- #


class FakeHook:
    """A minimal hook exposing the lifecycle surface: func_addr, saved_bytes, restore(mem)."""

    def __init__(self, func_addr: int, saved_bytes: bytes, *, fail: bool = False) -> None:
        self.func_addr = func_addr
        self.saved_bytes = saved_bytes
        self.fail = fail
        self.restored = False

    def restore(self, mem) -> None:
        if self.fail:
            raise RuntimeError("simulated restore failure")
        self.restored = True
        mem.write(self.func_addr, self.saved_bytes)


class FakeConsole:
    """Captures console.print output so warnings/errors can be asserted on."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, msg: str = "") -> None:
        self.lines.append(msg)


def test_hook_session_normal_exit_restores_all_and_clears_journal():
    mem = FakeMem()
    h1 = FakeHook(0x500000, SAVED_A)
    h2 = FakeHook(0x600000, SAVED_B)
    console = FakeConsole()

    with hookjournal.hook_session(mem, 1234, [h1, h2], console=console) as stop:
        assert not stop.is_set()
        # journal written for the active hooks while the session is live
        data = json.loads(hookjournal.JOURNAL_PATH.read_text(encoding="utf-8"))
        assert data["pid"] == 1234
        assert {e["addr"] for e in data["hooks"]} == {0x500000, 0x600000}

    assert h1.restored and h2.restored  # every hook restored on normal exit
    assert (0x500000, SAVED_A) in mem.writes
    assert (0x600000, SAVED_B) in mem.writes
    assert not hookjournal.JOURNAL_PATH.exists()  # cleared only after a FULL success


def test_hook_session_sigterm_handler_sets_stop_and_restores():
    """A SIGTERM during the session flips `stop` (no raise); exit still restores + clears."""
    mem = FakeMem()
    h = FakeHook(0x500000, SAVED_A)
    console = FakeConsole()

    orig = signal.getsignal(signal.SIGTERM)
    try:
        with hookjournal.hook_session(mem, 1234, [h], console=console) as stop:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler) and handler is not orig  # our graceful handler installed
            handler(signal.SIGTERM, None)  # simulate the signal delivery
            assert stop.is_set()  # handler only flips stop — never raises
            assert stop.signaled is True  # ...and records that a TERMINATING signal arrived
    finally:
        # The CM must have restored the ORIGINAL handler on exit (no leaked closure).
        assert signal.getsignal(signal.SIGTERM) is orig
        signal.signal(signal.SIGTERM, orig)

    assert h.restored
    assert not hookjournal.JOURNAL_PATH.exists()


def test_hook_session_partial_restore_failure_keeps_journal():
    """If one hook's restore fails, the others still restore and the journal is KEPT for recovery."""
    mem = FakeMem()
    good = FakeHook(0x500000, SAVED_A)
    bad = FakeHook(0x600000, SAVED_B, fail=True)
    console = FakeConsole()

    with hookjournal.hook_session(mem, 1234, [good, bad], console=console):
        pass

    assert good.restored  # the good hook restored even though `bad` raised first/after
    assert hookjournal.JOURNAL_PATH.exists()  # partial failure -> journal kept for next run/clean
    assert any("restore failed" in line for line in console.lines)


def test_hook_session_journal_write_failure_warns_but_still_runs_and_restores(monkeypatch):
    """If write_journal raises OSError, the session still yields and still restores all hooks."""
    mem = FakeMem()
    h = FakeHook(0x500000, SAVED_A)
    console = FakeConsole()

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(hookjournal, "write_journal", _boom)

    entered = False
    with hookjournal.hook_session(mem, 1234, [h], console=console):
        entered = True  # the yield still happens despite the journal write failing

    assert entered
    assert h.restored  # finally still restored the hook -> no orphan
    assert any("crash-recovery journal unavailable" in line for line in console.lines)
    # No journal was ever written, so there's nothing to clear; clear_journal is a no-op.
    assert not hookjournal.JOURNAL_PATH.exists()
