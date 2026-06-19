"""Pointer-chain name reader — the cheap replacement for the AOB name scanner.

The AOB scanner (``names_loop``) finds names by pattern-scanning ~1 GB of game memory every tick,
which causes periodic microstutter. For names we have a DERIVED, stable pointer chain to, we can
instead resolve the name's address with a handful of pointer reads (~5 reads vs a 1 GB sweep) and
translate it in place. DQXGame.exe loads at a fixed image base under Wine (no ASLR), so a chain
expressed relative to that base survives game restarts.

The chains here are BUILD-SPECIFIC, exactly like an AOB signature: they encode concrete struct
offsets in a particular game build and must be re-derived (via the pointer-scan tooling) when the
game updates. A chain that no longer resolves after an update must DEGRADE GRACEFULLY — return None,
never raise — so the reader simply marks that kind "broken" and the CLI can advise ``--name-scan``.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass, field

from .names_loop import ScannerHandle, translate_and_write_name


@dataclass(frozen=True)
class NameChain:
    """A derived pointer chain from the module base to a name record (or an array of them).

    ``root_offset`` is added to the image base to get the chain's first pointer slot; each entry in
    ``offsets`` is a dereference-then-add step; ``name_offset`` is the byte offset from the final
    record to the JA name string. ``write_prefix`` is the control prefix the game expects prepended
    to the written name (e.g. the nameplate "\\x04"); "" for most.

    When ``stride`` is non-zero the chain resolves to the BASE of a fixed-stride array of records and
    the reader walks up to ``count`` slots (``record + k*stride + name_offset``), stopping at the
    first empty/garbage slot — this is how one chain covers a whole party (player + companions).
    ``stride == 0`` (the default) is a single name.
    """

    kind: str
    root_offset: int
    offsets: tuple[int, ...]
    name_offset: int
    write_prefix: str = ""
    stride: int = 0
    count: int = 1


# Derived, BUILD-SPECIFIC name chains (re-derive on a game update via the pointer-scan tooling — treat
# each like an AOB signature). The party chain reaches the WHOLE party array (player + companions);
# concierge/chat chains are TBD (those kinds still need the intrusive AOB scanner via --name-scan).
NAME_CHAINS: tuple[NameChain, ...] = (
    # party: the whole party-member array. A static global (base+0x1c998bc) holds a constant pointer
    # to a fixed "party manager" (0x02d79a88); from it [+0x68]->[+0x2c4]->[+0x50] reaches the array
    # base. Members are 0x300 apart, JA name at offset 0; slot 0 = the player, 1+ = AI companions.
    # Derived + validated across 8+ restart sessions and multiple zones via the snapshot/intersection
    # tooling (the manager pointer is the cross-session invariant; the array itself moves every run).
    NameChain("party", 0x1C998BC, (0x68, 0x2C4, 0x50), 0, "", stride=0x300, count=8),
)


def resolve_chain(mem, base: int, chain: NameChain) -> int | None:
    """Walk ``chain`` from the image ``base`` to its record address, or None if the chain is broken.

    ``ptr`` starts at ``base + root_offset``; each offset is "read a u32 pointer at ptr, and if it's
    non-null advance to that pointer + offset". Returns the RECORD address (the JA name is at
    ``record + chain.name_offset``). A null dereference -> None. Any read error (struct.error from a
    short read, OSError from an unmapped address) is swallowed to None: a chain that breaks after a
    game update must NEVER raise — the reader just marks the kind broken.
    """
    try:
        ptr = base + chain.root_offset
        for off in chain.offsets:
            v = mem.read_u32(ptr)
            if not v:
                return None
            ptr = v + off
        return ptr
    except (struct.error, OSError):
        return None


@dataclass
class ChainStats:
    """Accumulated chain-reader state, exposed so the CLI can warn on broken chains.

    ``resolved`` / ``broken`` are the SETS of chain kinds that, as of the latest tick, resolved to a
    record or failed to (so a chain that breaks after a game update lands in ``broken`` and the CLI
    can advise ``--name-scan``). ``written`` counts successful name writes; ``samples`` keeps a few
    (ja, en) pairs for logging.
    """

    ticks: int = 0
    written: int = 0
    resolved: set[str] = field(default_factory=set)
    broken: set[str] = field(default_factory=set)
    samples: list[tuple[str, str]] = field(default_factory=list)


def run_chains(
    mem,
    translator,
    base: int,
    chains: list[NameChain],
    *,
    stop: threading.Event,
    interval: float = 1.0,
    on_write=None,
    profiler=None,
) -> ChainStats:
    """Poll ``chains`` until ``stop`` is set, translating each resolved name in place.

    Each tick, for every chain: resolve it. A chain that fails to resolve (broken pointer / null
    deref) marks its kind ``broken`` and is skipped this tick. A resolved chain reads the JA name(s)
    at ``record + name_offset`` — a single name, or (when ``stride`` is set) every member of a
    fixed-stride array up to ``count``, stopping at the first empty/garbage slot — and translates+
    writes each Japanese one via the SHARED helper (the exact same code path as the AOB scanner).
    Resolved vs broken kinds are tracked on the returned ChainStats so the CLI can warn when a chain
    stops resolving (likely a game update).
    """
    stats = ChainStats()
    while not stop.is_set():
        stats.ticks += 1
        _t = time.monotonic() if profiler is not None else 0.0
        for chain in chains:
            rec = resolve_chain(mem, base, chain)
            if rec is None:
                stats.broken.add(chain.kind)
                stats.resolved.discard(chain.kind)
                continue
            stats.resolved.add(chain.kind)
            stats.broken.discard(chain.kind)
            for k in range(chain.count if chain.stride else 1):
                addr = rec + k * chain.stride + chain.name_offset
                # Array walk: stop at the first empty / non-text-binary slot (past the last member).
                # A real name (JA, or already-English after we translated it) is neither; the U+FFFD
                # replacement char means decode hit raw binary -> we've run off the end of the party.
                if chain.stride:
                    try:
                        raw = mem.read_cstring(addr, 64)
                    except (struct.error, OSError):
                        break
                    if not raw or "�" in raw:
                        break
                written = translate_and_write_name(
                    mem, translator, addr, chain.write_prefix, on_write=on_write
                )
                if written is not None:
                    stats.written += 1
                    if len(stats.samples) < 10 and written not in stats.samples:
                        stats.samples.append(written)
        if profiler is not None:
            profiler.record(
                "namechain", "poll", time.monotonic() - _t,
                f"resolved={len(stats.resolved)} broken={len(stats.broken)}",
            )
        stop.wait(interval)
    return stats


def start_chain_reader(
    mem,
    translator,
    base: int | None,
    *,
    enabled: bool,
    chains: list[NameChain] | None = None,
    interval: float = 1.0,
    on_write=None,
    profiler=None,
) -> ScannerHandle:
    """Start the pointer-chain name reader as a DAEMON thread for ONE game attach, return a handle.

    Mirrors ``names_loop.start_scanner`` (and reuses its ``ScannerHandle``): a private stop Event +
    a daemon thread bound to THIS attach's ``mem``, stopped+joined by the caller right after serve()
    returns and BEFORE any re-attach (the chain reader keys off its OWN stop, not the supervisor's,
    for the same game-gone reason the scanner does).

    No thread starts (``thread=None``, so ``stop_and_join`` is a safe no-op) when the reader is
    disabled, the image ``base`` is None (couldn't resolve the module — likely a game update), or
    there are no chains to read.
    """
    stop = threading.Event()
    chains = chains if chains is not None else NAME_CHAINS
    if not enabled or base is None or not chains:
        return ScannerHandle(stop=stop, thread=None)
    thread = threading.Thread(
        target=run_chains,
        args=(mem, translator, base, chains),
        kwargs={"stop": stop, "interval": interval, "on_write": on_write, "profiler": profiler},
        name="name-chain-reader",
        daemon=True,  # never block process exit; the caller stop+joins it explicitly anyway
    )
    thread.start()
    return ScannerHandle(stop=stop, thread=thread)
