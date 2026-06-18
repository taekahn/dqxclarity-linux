"""Tests for the pointer-chain name reader (cheap replacement for the AOB name scanner).

Covers:
  * memory_linux.parse_module_base / LinuxProcessMemory.module_base — the image-base parser that
    tolerates space-containing module paths and keys on the file-offset-0 mapping.
  * name_chains.resolve_chain — walks a scripted pointer graph to the record addr; a null deref or a
    read error returns None and NEVER raises.
  * name_chains.run_chains — a resolvable chain to a JA name is translated+written; a broken chain
    writes nothing and is flagged broken on the stats.
  * name_chains.start_chain_reader — the per-attach daemon-thread helper (mirrors start_scanner):
    enabled -> a thread runs run_chains; disabled / base None / no chains -> no thread, no-op join.
"""

from __future__ import annotations

import struct
import threading

from dqxclarity.process.memory_linux import LinuxProcessMemory, parse_module_base
from dqxclarity.runtime import name_chains
from dqxclarity.runtime.name_chains import NameChain


# =============================================================================================== #
# module_base — image-base parser (space-containing paths; file-offset-0 mapping)                  #
# =============================================================================================== #

# A realistic maps excerpt: the DQXGame.exe path contains spaces ("DRAGON QUEST X"), so a plain
# split() would shred it. The offset-0 line is the image base; an earlier non-zero-offset line for
# the SAME file must NOT be picked.
_MAPS = (
    "00010000-00011000 r--p 00000000 00:00 0    [vvar]\n"
    "003f0000-00400000 r--p 00001000 08:01 123  /games/SteamLibrary/DRAGON QUEST X/Game/DQXGame.exe\n"
    "00400000-01c00000 r-xp 00000000 08:01 123  /games/SteamLibrary/DRAGON QUEST X/Game/DQXGame.exe\n"
    "01c00000-01d00000 rw-p 01800000 08:01 123  /games/SteamLibrary/DRAGON QUEST X/Game/DQXGame.exe\n"
    "7f000000-7f001000 r-xp 00000000 08:01 99   /usr/lib/wine/ntdll.dll\n"
)


def test_parse_module_base_picks_offset_zero_line_with_spaces_in_path():
    # The image base is the file-offset-0 mapping (0x400000), NOT the lower non-zero-offset line
    # (0x3f0000) that merely has a smaller address — and the spaced path must be matched whole.
    assert parse_module_base(_MAPS, "DQXGame.exe") == 0x400000


def test_parse_module_base_absent_returns_none():
    assert parse_module_base(_MAPS, "NoSuchModule.exe") is None
    assert parse_module_base("", "DQXGame.exe") is None


def test_parse_module_base_matches_arbitrary_suffix():
    assert parse_module_base(_MAPS, "ntdll.dll") == 0x7F000000


def test_module_base_reads_maps_and_returns_base(monkeypatch):
    # LinuxProcessMemory.module_base reads /proc/<pid>/maps and parses it. Monkeypatch the read so
    # no real process is needed; a space-containing path still resolves to its offset-0 base.
    import dqxclarity.process.memory_linux as mem_mod

    class _FakePath:
        def __init__(self, p):
            self._p = p

        def read_text(self):
            assert self._p == "/proc/4321/maps"
            return _MAPS

    monkeypatch.setattr(mem_mod, "Path", _FakePath)
    mem = LinuxProcessMemory(4321)
    assert mem.module_base() == 0x400000
    assert mem.module_base("DQXGame.exe") == 0x400000


def test_module_base_unreadable_maps_returns_none(monkeypatch):
    import dqxclarity.process.memory_linux as mem_mod

    class _BoomPath:
        def __init__(self, p):
            pass

        def read_text(self):
            raise OSError("no such file")

    monkeypatch.setattr(mem_mod, "Path", _BoomPath)
    assert LinuxProcessMemory(1).module_base() is None


# =============================================================================================== #
# resolve_chain — walk a scripted pointer graph                                                    #
# =============================================================================================== #


class _GraphMem:
    """Stub mem whose read_u32 walks a scripted pointer graph: {addr: value}. An address not in the
    graph reads 0 (a null deref). Optionally raises to prove resolve_chain swallows the error."""

    def __init__(self, graph, *, raise_at=None):
        self._graph = graph
        self._raise_at = raise_at

    def read_u32(self, addr):
        if self._raise_at is not None and addr == self._raise_at:
            raise struct.error("short read")
        return self._graph.get(addr, 0)


def test_resolve_chain_walks_to_record_addr():
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4, 0x8), 57, "")
    # ptr0 = base + 0x1000 = 0x401000. read_u32 -> 0x500000 ; ptr1 = 0x500000 + 0x4 = 0x500004
    # read_u32(0x500004) -> 0x600000 ; ptr2 = 0x600000 + 0x8 = 0x600008  (the record addr)
    graph = {0x401000: 0x500000, 0x500004: 0x600000}
    mem = _GraphMem(graph)
    assert name_chains.resolve_chain(mem, base, chain) == 0x600008


def test_resolve_chain_null_deref_returns_none():
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4, 0x8), 57, "")
    # First deref reads 0 (0x401000 not in graph) -> None, never raises.
    mem = _GraphMem({})
    assert name_chains.resolve_chain(mem, base, chain) is None


def test_resolve_chain_mid_chain_null_returns_none():
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4, 0x8), 57, "")
    # First deref ok, second reads 0 -> None.
    mem = _GraphMem({0x401000: 0x500000})  # 0x500004 missing -> 0
    assert name_chains.resolve_chain(mem, base, chain) is None


def test_resolve_chain_read_error_returns_none_never_raises():
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4, 0x8), 57, "")
    mem = _GraphMem({0x401000: 0x500000}, raise_at=0x500004)
    # A struct.error mid-walk (broken chain after a game update) must be swallowed to None.
    assert name_chains.resolve_chain(mem, base, chain) is None


# =============================================================================================== #
# run_chains — translate+write a resolved name; flag a broken chain                                #
# =============================================================================================== #


class _OneTickStop:
    """Lets run_chains execute exactly one poll iteration."""

    def __init__(self):
        self._n = 0

    def is_set(self):
        self._n += 1
        return self._n > 1

    def wait(self, _t):
        pass


class _ChainTranslator:
    player_name_ja = None
    player_name_en = None
    sibling_name_ja = None
    sibling_name_en = None

    def translate_name(self, ja):
        return "romaji(" + ja + ")"


class _ResolvingMem:
    """Mem that resolves the party chain to a record and serves a JA name at record+name_offset."""

    def __init__(self, base, chain, ja):
        self._base = base
        self._chain = chain
        self._ja = ja
        self.writes = []
        # Build a graph that makes resolve_chain land on a known record address.
        self._record = 0x700000
        ptr = base + chain.root_offset
        self._graph = {}
        cur = 0x500000
        for off in chain.offsets[:-1]:
            self._graph[ptr] = cur
            ptr = cur + off
            cur += 0x100000
        # Final offset lands on the record.
        self._graph[ptr] = self._record - chain.offsets[-1]
        self._name_addr = self._record + chain.name_offset

    def read_u32(self, addr):
        return self._graph.get(addr, 0)

    def read_cstring(self, addr, n=64):
        return self._ja if addr == self._name_addr else ""

    def write_cstring(self, addr, text, *, max_bytes):
        self.writes.append((addr, text))
        return True


def test_run_chains_translates_and_writes_resolved_name():
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4, 0x8, 0x10), 57, "")
    mem = _ResolvingMem(base, chain, "スライム")
    stats = name_chains.run_chains(
        mem, _ChainTranslator(), base, [chain], stop=_OneTickStop(), interval=0
    )
    assert mem.writes, "a resolved JA name must be translated+written"
    addr, text = mem.writes[0]
    assert addr == mem._name_addr
    assert "romaji(スライム)" in text
    assert "party" in stats.resolved
    assert "party" not in stats.broken
    assert stats.written == 1


class _BrokenMem:
    """Mem whose chain derefs to 0 immediately -> resolve_chain returns None (broken chain)."""

    def __init__(self):
        self.writes = []

    def read_u32(self, addr):
        return 0  # null deref on the very first step

    def read_cstring(self, addr, n=64):
        return "スライム"  # would translate IF we ever reached it (we must not)

    def write_cstring(self, addr, text, *, max_bytes):
        self.writes.append((addr, text))
        return True


def test_run_chains_broken_chain_no_write_and_flagged():
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4,), 57, "")
    mem = _BrokenMem()
    stats = name_chains.run_chains(
        mem, _ChainTranslator(), base, [chain], stop=_OneTickStop(), interval=0
    )
    assert mem.writes == [], "a broken chain must write nothing"
    assert "party" in stats.broken
    assert "party" not in stats.resolved
    assert stats.written == 0


def test_run_chains_player_name_precedence():
    """The shared helper's player/sibling precedence applies through the chain path too."""
    base = 0x400000
    chain = NameChain("party", 0x1000, (0x4, 0x8, 0x10), 57, "")
    mem = _ResolvingMem(base, chain, "タイカン")

    class _Pinned(_ChainTranslator):
        player_name_ja = "タイカン"
        player_name_en = "Taikan"

        def translate_name(self, ja):
            return "Squid"  # the colliding cache hit the pin must beat

    name_chains.run_chains(mem, _Pinned(), base, [chain], stop=_OneTickStop(), interval=0)
    assert mem.writes
    _, text = mem.writes[0]
    assert "Taikan" in text and "Squid" not in text


# =============================================================================================== #
# start_chain_reader — per-attach daemon-thread helper                                             #
# =============================================================================================== #


def test_start_chain_reader_enabled_runs_run_chains(monkeypatch):
    seen = {}
    started = threading.Event()

    def fake_run_chains(mem, translator, base, chains, *, stop, interval, on_write=None, profiler=None):
        seen.update(mem=mem, translator=translator, base=base, chains=chains, stop=stop, interval=interval)
        started.set()
        stop.wait()

    monkeypatch.setattr(name_chains, "run_chains", fake_run_chains)

    mem = object()
    translator = object()
    handle = name_chains.start_chain_reader(mem, translator, 0x400000, enabled=True, interval=0.25)

    assert started.wait(2.0), "the chain reader thread never invoked run_chains"
    assert handle.thread is not None and handle.thread.daemon is True
    assert seen["mem"] is mem and seen["translator"] is translator
    assert seen["base"] == 0x400000
    assert seen["chains"] is name_chains.NAME_CHAINS  # defaults to the module chain list
    assert seen["interval"] == 0.25
    assert seen["stop"] is handle.stop

    handle.stop_and_join(timeout=2.0)
    assert handle.stop.is_set()
    assert not handle.thread.is_alive()


def test_start_chain_reader_disabled_starts_no_thread(monkeypatch):
    called = []
    monkeypatch.setattr(name_chains, "run_chains", lambda *a, **k: called.append(1))
    handle = name_chains.start_chain_reader(object(), object(), 0x400000, enabled=False)
    assert handle.thread is None
    handle.stop_and_join(timeout=1.0)  # safe no-op
    assert called == []


def test_start_chain_reader_none_base_starts_no_thread(monkeypatch):
    called = []
    monkeypatch.setattr(name_chains, "run_chains", lambda *a, **k: called.append(1))
    # base None (couldn't resolve the module — likely a game update) -> no thread.
    handle = name_chains.start_chain_reader(object(), object(), None, enabled=True)
    assert handle.thread is None
    handle.stop_and_join(timeout=1.0)
    assert called == []


def test_start_chain_reader_empty_chains_starts_no_thread(monkeypatch):
    called = []
    monkeypatch.setattr(name_chains, "run_chains", lambda *a, **k: called.append(1))
    handle = name_chains.start_chain_reader(object(), object(), 0x400000, enabled=True, chains=[])
    assert handle.thread is None
    handle.stop_and_join(timeout=1.0)
    assert called == []


def test_name_chains_default_covers_party():
    kinds = {c.kind for c in name_chains.NAME_CHAINS}
    assert "party" in kinds
    party = next(c for c in name_chains.NAME_CHAINS if c.kind == "party")
    assert party.root_offset == 0x1C95FA0
    assert party.offsets == (0x4, 0x4A4, 0x377)
    assert party.name_offset == 57
    assert party.write_prefix == ""
