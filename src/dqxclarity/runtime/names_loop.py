"""Live name-translation loop (Phase 3a, scanner-based — no hooks).

Periodically scans the game's memory for the name patterns, romanizes/translates each Japanese
name locally, and writes the result back into the buffer. This is the polling approach upstream
uses for names; it needs no code hooking, just the Phase 2 scanner + the translation pipeline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..process.memory_linux import LinuxProcessMemory, MapRegion
from ..process.signatures import NAME_PATTERNS
from ..translate.pipeline import Translator

# How often we force a FULL sweep of all writable data regions to (re)discover where names live.
# Between sweeps we only rescan the small "warm" regions that already yielded hits, which is what
# makes the loop cheap. A module constant so tests can shrink it to drive the timer deterministically.
FULL_RESCAN_SECS = 20.0


def _is_japanese(text: str) -> bool:
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯" for c in text)


def _region_for(addr: int, regions: list[MapRegion]) -> MapRegion | None:
    """Map a hit address back to the MapRegion that contains it (start <= addr < end).

    Linear scan — ``regions`` is the full writable-data list from a sweep (a few hundred entries
    at most), and we only call this for the handful of hits per pass, so a sort+bisect buys nothing.
    """
    for r in regions:
        if r.start <= addr < r.end:
            return r
    return None


@dataclass
class LoopStats:
    scans: int = 0
    seen: int = 0
    written: int = 0
    full_scans: int = 0  # ticks that did an expensive full-heap sweep
    warm_scans: int = 0  # ticks that only rescanned the warm regions (the cheap common case)
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
    profiler=None,
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
        kwargs={"stop": stop, "interval": interval, "on_write": on_write, "profiler": profiler},
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
    profiler=None,
) -> LoopStats:
    """Run until ``stop`` is set. Returns accumulated stats.

    DISCOVERY vs MAINTENANCE. The naive loop did a FULL sweep of every writable data region
    (hundreds of MB) for each pattern EVERY tick, which competes with the game for memory bandwidth
    and lags it. But in steady state nothing new appears: party members don't change while you run
    around, and a name we already translated is now English in memory (it no longer matches
    ``_is_japanese``). So we split the work:

      * MAINTENANCE (most ticks): rescan ONLY the "warm" regions — the regions that yielded a name
        hit on the previous pass (usually 1-3 small buffers). Cheap. This still catches a name that
        changes in place (a nameplate flipping back to JA) every single tick.
      * DISCOVERY (rare): a FULL sweep over all data regions, to (re)find where names live now and
        refresh the warm set.

    A full sweep is triggered when:
      (a) the warm set is empty AND a backoff timer has elapsed — i.e. we have nowhere cheap to
          look and we're allowed to go hunting again (the backoff stops us from full-sweeping every
          tick when there are genuinely no names on screen — extremely common when running through
          empty areas);
      (b) the periodic FULL_RESCAN_SECS timer elapsed — a safety net so a name appearing in a brand
          new region is picked up within ~FULL_RESCAN_SECS even if the warm set is non-empty; or
      (c) a warm-only pass this tick returned ZERO hits while the warm set was non-empty — the
          buffers we were watching just emptied/moved (classic zone change), so rediscover NOW
          rather than waiting out the timer. We do this rediscovery ONCE (not every tick): if the
          full sweep then also finds nothing, the warm set is empty and case (a)'s backoff governs.
    """
    stats = LoopStats()
    warm_regions: list[MapRegion] = []
    # Region list captured by the most recent FULL sweep — used to map hit addresses back to their
    # owning region. Warm-only passes reuse this (warm regions are a subset of it); we only refresh
    # it on a full sweep, which is the only time the region layout could meaningfully change for us.
    last_full_regions: list[MapRegion] = []
    # next_full_at: monotonic deadline for the periodic full sweep (case b).
    # backoff_until: monotonic deadline gating discovery when the warm set is empty (case a). Set
    # after a full sweep finds nothing so we don't hammer the heap tick after tick.
    next_full_at = 0.0  # 0 -> the very first tick is always a full sweep (warm set starts empty)
    backoff_until = 0.0

    while not stop.is_set():
        stats.scans += 1
        now = time.monotonic()

        # ---- decide full vs warm for THIS tick ---------------------------------------------- #
        # (b) periodic safety-net sweep, or (a) warm set empty and backoff elapsed.
        do_full = (now >= next_full_at) or (not warm_regions and now >= backoff_until)

        # A non-full tick with no warm regions has nothing to scan and nothing to rediscover (we're
        # in backoff). Skip the work entirely and wait out the interval.
        if not do_full and not warm_regions:
            stop.wait(interval)
            continue

        _t_scan = time.monotonic() if profiler is not None else 0.0
        if profiler is not None:
            profiler.scanning = True  # mark the window so serve-loop gaps get attributed to the scan
        all_hits: list[int] = []  # every match address found this pass, for warm-set recomputation
        if do_full:
            stats.full_scans += 1
            last_full_regions = mem.scannable_regions(data_only=True)
            scan_regions = last_full_regions
        else:
            stats.warm_scans += 1
            scan_regions = warm_regions

        for np in NAME_PATTERNS:
            for match in mem.pattern_scan(np.pattern, limit=200, regions=scan_regions) or []:
                all_hits.append(match)
                name_addr = match + np.name_offset
                ja = mem.read_cstring(name_addr, 64)
                if not ja or not _is_japanese(ja):
                    continue
                stats.seen += 1
                # Player/sibling substitution FIRST — the player's OWN name in the party buffer
                # (e.g. タイカン) collides with a cached monster name ("Squid"), so an exact match on
                # the live player/sibling JA name must beat the cache lookup. Same precedence as
                # dispatch._translate_name_runs.resolve(); without it the player's party nameplate
                # renders as the colliding monster name.
                if ja == translator.player_name_ja and translator.player_name_en:
                    en = translator.player_name_en
                elif ja == translator.sibling_name_ja and translator.sibling_name_en:
                    en = translator.sibling_name_en
                else:
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

        if profiler is not None:
            profiler.scanning = False
            profiler.record(
                "namescan", "full" if do_full else "warm", time.monotonic() - _t_scan,
                f"regions={len(scan_regions)} hits={len(all_hits)}",
            )

        # ---- refresh state from this pass's results ----------------------------------------- #
        # (c) Zone-change rediscovery: a warm-only pass that came up empty means the buffers we were
        # watching moved or freed. Drop the (now useless) warm set and let the NEXT iteration's
        # do_full decision fire immediately (warm empty + backoff not armed -> full sweep). We do
        # NOT loop-continue here; we just fall through, and because we leave backoff_until alone the
        # next tick rediscovers at once.
        if not do_full and warm_regions and not all_hits:
            warm_regions = []
            continue  # skip the timer bookkeeping below; rediscover on the very next tick

        # Recompute the warm set = the distinct regions that contained at least one hit this pass.
        new_warm: list[MapRegion] = []
        seen_starts: set[int] = set()
        for addr in all_hits:
            r = _region_for(addr, last_full_regions)
            if r is not None and r.start not in seen_starts:
                seen_starts.add(r.start)
                new_warm.append(r)
        warm_regions = new_warm

        if do_full:
            # Arm the next periodic sweep regardless of outcome.
            next_full_at = now + FULL_RESCAN_SECS
            if not warm_regions:
                # No names anywhere: back off so we don't full-sweep the whole heap every tick while
                # running through an empty area. Discovery is gated until this deadline (case a).
                backoff_until = now + FULL_RESCAN_SECS

        stop.wait(interval)
    return stats
