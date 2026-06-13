"""Live name-translation loop (Phase 3a, scanner-based — no hooks).

Periodically scans the game's memory for the name patterns, romanizes/translates each Japanese
name locally, and writes the result back into the buffer. This is the polling approach upstream
uses for names; it needs no code hooking, just the Phase 2 scanner + the translation pipeline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..process.memory_linux import LinuxProcessMemory
from ..process.signatures import NAME_PATTERNS
from ..translate.pipeline import Translator


def _is_japanese(text: str) -> bool:
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯" for c in text)


@dataclass
class LoopStats:
    scans: int = 0
    seen: int = 0
    written: int = 0
    samples: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ScannerHandle:
    """Live handle to a background name-scanner thread (one per game attach).

    ``run`` starts one of these inside each attach's ``hook_session`` block and ``.stop()``s it the
    moment ``serve()`` returns — see ``start_scanner`` for the game-gone lifecycle reasoning. When
    the scanner is disabled (``--no-names``) ``start_scanner`` returns a handle with ``thread=None``
    so the caller's ``.stop()`` is an unconditional no-op (uniform call site, no None-guard).
    """

    stop: threading.Event
    thread: threading.Thread | None = None

    def stop_and_join(self, timeout: float | None = None) -> None:
        """Set the per-attach stop and join the thread. Safe to call when disabled (no thread)."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)


def start_scanner(
    mem: LinuxProcessMemory,
    translator: Translator,
    *,
    enabled: bool,
    interval: float = 1.0,
    on_write=None,
) -> ScannerHandle:
    """Start the polling name scanner as a DAEMON thread for ONE game attach, return a handle.

    Why a per-attach thread with its OWN stop Event (not the supervisor's shared ``stop``):
    ``serve()`` returns either on a user stop (which DOES flip the shared ``stop``) or on a
    game-gone (which sets ``game_gone`` but NOT ``stop``). If the scanner keyed off the shared
    ``stop``, a game-gone would leave it spinning ``pattern_scan`` against a dead pid forever.
    Reads on a dead pid fail gracefully (empty result, no raise), so it wouldn't crash — but it
    would never stop and would churn until the next attach. So each attach gets a fresh
    ``names_stop`` Event; ``run`` sets it + joins the thread right after ``serve()`` returns,
    BEFORE re-attaching builds a new ``mem``. The thread is bound to THIS attach's ``mem``, exactly
    like the hooks.

    When ``enabled`` is False (``--no-names``) no thread is started; the returned handle's
    ``stop_and_join`` is a no-op so the call site stays uniform.
    """
    stop = threading.Event()
    if not enabled:
        return ScannerHandle(stop=stop, thread=None)
    thread = threading.Thread(
        target=run,
        args=(mem, translator),
        kwargs={"stop": stop, "interval": interval, "on_write": on_write},
        name="name-scanner",
        daemon=True,  # never block process exit on it; run() always stop+joins it explicitly anyway
    )
    thread.start()
    return ScannerHandle(stop=stop, thread=thread)


def run(
    mem: LinuxProcessMemory,
    translator: Translator,
    *,
    stop: threading.Event,
    interval: float = 1.0,
    on_write=None,
) -> LoopStats:
    """Run until ``stop`` is set. Returns accumulated stats."""
    stats = LoopStats()
    while not stop.is_set():
        stats.scans += 1
        for np in NAME_PATTERNS:
            for match in mem.pattern_scan(np.pattern, data_only=True, limit=200) or []:
                name_addr = match + np.name_offset
                ja = mem.read_cstring(name_addr, 64)
                if not ja or not _is_japanese(ja):
                    continue
                stats.seen += 1
                en = translator.translate_name(ja)
                if not en or en == ja:
                    continue
                # Re-read guard against the value changing between scan and write.
                if mem.read_cstring(name_addr, 64) != ja:
                    continue
                # Budget = the JA name's byte span (+NUL), plus the control prefix the game
                # expects prepended (e.g. \x04) which doesn't count against the name field.
                budget = len(ja.encode()) + 1 + len(np.write_prefix.encode())
                if mem.write_cstring(name_addr, np.write_prefix + en, max_bytes=budget):
                    stats.written += 1
                    if len(stats.samples) < 10 and (ja, en) not in stats.samples:
                        stats.samples.append((ja, en))
                    if on_write:
                        on_write(ja, en)
        stop.wait(interval)
    return stats
