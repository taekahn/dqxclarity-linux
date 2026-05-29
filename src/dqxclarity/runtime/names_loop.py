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
