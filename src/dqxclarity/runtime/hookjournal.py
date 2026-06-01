"""Persistent "active hooks" journal — recover orphaned detours after an UNCLEAN exit.

A graceful exit (SIGINT/SIGTERM/SIGHUP) runs the CLI's ``finally`` and restores every patched
function prologue. But a SIGKILL or a hard crash kills the process with no chance to run any
handler, so the game keeps the detour jmps in place: every subsequent call spins the cave's
blocking shellcode to its timeout (a multi-second stall that looks like a freeze/crash).

To survive that, ``run`` writes a small JSON journal of the installed hooks (each function's
address + the original stolen prologue bytes) the moment they're installed, and ``recover_orphans``
restores them on the NEXT run (or via ``dqxclarity clean``).

PID SAFETY (the single most important property): the journal records the game's pid. The next
recovery only writes the saved bytes back if the journal's pid still matches the current game pid.
If the pid differs, the patched process is gone and those addresses now belong to a DIFFERENT
process — writing the old bytes there would corrupt or crash it. In that case we write NOTHING and
just discard the journal.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from contextlib import contextmanager

from .. import config

JOURNAL_PATH = config.CONFIG_DIR / "active_hooks.json"


def write_journal(game_pid: int, entries: list[tuple[int, bytes]]) -> None:
    """Atomically record the active hooks for ``game_pid``.

    ``entries`` is a list of (func_addr, saved_bytes). Written to a temp file in the same dir
    then ``os.replace``d onto JOURNAL_PATH so a crash mid-write can never leave a partial journal.
    """
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": game_pid,
        "hooks": [{"addr": addr, "bytes": saved.hex()} for addr, saved in entries],
    }
    tmp = JOURNAL_PATH.with_suffix(JOURNAL_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, JOURNAL_PATH)


def clear_journal() -> None:
    """Delete JOURNAL_PATH if it exists (no-op when missing)."""
    try:
        JOURNAL_PATH.unlink()
    except FileNotFoundError:
        pass


def recover_orphans(mem, game_pid: int) -> list[int]:
    """Restore any detours left by a previous unclean exit. Returns the restored addresses.

    Safety-critical. Flow:
      * no journal -> return [] (nothing to do).
      * journal pid != ``game_pid`` -> the patched process died and its addresses now belong to a
        DIFFERENT process; writing the old saved bytes would corrupt it, so DO NOTHING but discard
        the stale journal and return [].
      * journal pid == ``game_pid`` -> for each entry, read 1 byte at the address; only when it is
        still ``0xE9`` (a detour jmp is present) write the saved prologue bytes back and record the
        address. A non-0xE9 byte means the prologue is already clean (or never ours), so skip it.
      * always clear the journal at the end.
    """
    if not JOURNAL_PATH.is_file():
        return []
    try:
        data = json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable/corrupt journal: we can't trust it, so don't write anything; just discard it.
        clear_journal()
        return []

    journal_pid = data.get("pid")
    # PID-SAFETY GUARD (paramount): never write into a process we didn't patch. The strict
    # isinstance check is load-bearing: a hand-edited float/str/None pid must NEVER pass, because
    # Python's `1234.0 == 1234` is True — a float pid would otherwise sneak past the equality test
    # and let us write into a process whose pid we never actually verified.
    if not isinstance(journal_pid, int) or journal_pid != game_pid:
        clear_journal()
        return []

    restored: list[int] = []
    for entry in data.get("hooks", []):
        try:
            addr = entry["addr"]
            saved = bytes.fromhex(entry["bytes"])
            head = mem.read(addr, 1)
            if head[:1] == b"\xe9":  # a detour jmp is still present -> restore the prologue
                mem.write(addr, saved)
                restored.append(addr)
        except Exception:  # noqa: BLE001 — one bad entry must not abort the rest of recovery
            continue
    clear_journal()
    return restored


@contextmanager
def hook_session(mem, game_pid: int, hooks, *, console):
    """Lifecycle for a set of installed hooks: crash-recovery journal + signal-safe restore.

    Wraps the full orphan-safety lifecycle so ``run`` and ``translate-dialogue`` (and any future
    command that installs detours) share ONE implementation of these subtle, load-bearing rules:

      * Installs SIGTERM/SIGHUP handlers that only flip the yielded ``stop`` event (never raise —
        raising could interrupt a mid-write). ``kill <pid>``, a terminal close, or ``systemd stop``
        then unwind through the ``finally`` and restore every hook, exactly like Ctrl-C (SIGINT).
        The previous handlers are saved and RESTORED on exit so we don't leak a handler that closes
        over a finished session's stop event.
      * Writes the crash-recovery journal (func addr + saved prologue bytes per hook) so a SIGKILL
        or hard crash — which no handler can catch — is recovered on the NEXT run/clean via the
        0xE9 guard. If the journal write fails (disk full / EACCES) we WARN and continue: the
        session still runs and still restores on a clean/SIGTERM exit; only unclean-exit recovery
        is lost. The write must never be allowed to abort the session and orphan the hooks.
      * On exit restores EVERY hook even if one ``restore`` raises (per-hook fault tolerance), and
        clears the journal ONLY if all restores succeeded — a partial failure leaves the journal so
        the next run/clean recovers the rest via the 0xE9 guard.

    ``hooks`` is any list of objects exposing ``.func_addr``, ``.saved_bytes`` and ``.restore(mem)``.
    Yields the ``stop`` :class:`threading.Event` the command's serve loop / duration Timer drive.

    NOTE: the caller is still responsible for calling :func:`recover_orphans` BEFORE installing the
    hooks (recovering a PREVIOUS unclean exit's detours); this CM only governs the CURRENT session.
    """
    stop = threading.Event()
    # `signaled` distinguishes a terminating SIGNAL (SIGTERM/SIGHUP) from any OTHER reason `stop`
    # gets set — e.g. a supervisory caller flips `stop` because the GAME vanished. A re-attach loop
    # consults this so it EXITS on a real signal instead of mistaking it for a game-gone event and
    # looping forever (a `kill <pid>` that lands in the same tick the game closes must still stop us).
    stop.signaled = False
    orig_term = signal.getsignal(signal.SIGTERM)
    orig_hup = signal.getsignal(signal.SIGHUP)

    def _graceful(signum, frame):  # noqa: ANN001, ARG001
        stop.signaled = True
        stop.set()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGHUP, _graceful)
    try:
        write_journal(game_pid, [(h.func_addr, h.saved_bytes) for h in hooks])
    except OSError as e:
        console.print(f"[yellow]crash-recovery journal unavailable this session: {e}[/]")
    try:
        yield stop
    finally:
        failed = False
        for h in hooks:
            try:
                h.restore(mem)
            except Exception as e:  # noqa: BLE001 — restore every hook even if one fails
                console.print(f"[red]restore failed: {e}[/]")
                failed = True
        signal.signal(signal.SIGTERM, orig_term)
        signal.signal(signal.SIGHUP, orig_hup)
        # Clear the journal ONLY on a full success — a partial failure leaves it so the next
        # run/clean recovers the un-restored hooks via the 0xE9 guard.
        if not failed:
            clear_journal()
