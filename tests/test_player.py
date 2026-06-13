"""Tests for the PLAYER login hook — a READ-ONLY hook that auto-detects the player's and sibling's
names from the game's login struct and applies them so name<->placeholder substitution works
without manual config.

CRITICAL INVARIANT under test: the hook NEVER writes into the game's memory except the STATE-release
dword. A bug that wrote into the struct could corrupt/crash the game. Several tests assert that the
only game write is the STATE release.

Covers (offline — no game needed):
  * find_function resolves PLAYER_SIG; the HOOKS["player"] spec profile (player=True, stolen_len=6,
    capture==CAPTURE_ARG0, prologue_verify 55 8b ec);
  * PlayerHook.serve_once: reads player@+24 / sibling@+100 / relationship@+119, calls apply_names,
    releases STATE to DONE, and writes NOTHING into the game (only the STATE dword);
  * serve_once releases STATE even if apply_names raises, and no-ops when not requested;
  * build_apply_names: updates the live translator names + saves cfg, romaji guarded by is_available,
    no redundant save when unchanged;
  * LIVE update: a community_lookup built BEFORE apply_names picks up the NEW name AFTER it runs.
"""

from __future__ import annotations

import struct
import threading

from types import SimpleNamespace

from dqxclarity.process import detour
from dqxclarity.process import signatures as sig
from dqxclarity.process.detour import (
    STATE_DONE,
    STATE_REQUEST,
    CAPTURE_ARG0,
    PlayerHook,
)
from dqxclarity.process.hooks import HOOKS, HookSpec, find_function
from dqxclarity.runtime.dispatch import _make_community_lookup, serve
from dqxclarity.runtime.playernames import build_apply_names
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator


# ---------------------------------------------------------------- find_function / spec profile

class FakeScanMem:
    """Stubs pattern_scan (preset matches) and read (preset prologue bytes). Mirrors test_hooks."""

    def __init__(self, matches, prologue) -> None:
        self._matches = matches
        self._prologue = prologue

    def pattern_scan(self, pattern, *, data_only=False, limit=4):  # noqa: ARG002
        return self._matches

    def read(self, addr: int, size: int) -> bytes:
        return self._prologue.get(addr, b"")[:size]


def test_player_find_function_match_is_func_and_prologue_verifies():
    p = HOOKS["player"]
    assert p.prologue_back == 0 and p.prologue_verify == b"\x55\x8b\xec"
    match = 0x00FD6D10
    mem = FakeScanMem([match], {match: b"\x55\x8b\xec"})
    assert find_function(mem, p) == match


def test_player_find_function_rejects_wrong_prologue():
    match = 0x00FD6D10
    mem = FakeScanMem([match], {match: b"\x90\x90\x90"})  # not 55 8b ec
    assert find_function(mem, HOOKS["player"]) is None


def test_player_hookspec_profile():
    p = HOOKS["player"]
    assert isinstance(p, HookSpec)
    assert p.player is True
    assert p.return_hook is False and p.is_name is False
    assert p.stolen_len == sig.PLAYER_STOLEN_LEN == 6
    assert p.signature == sig.PLAYER_SIG
    assert p.capture == CAPTURE_ARG0 == detour.CAPTURE_ARG0
    assert p.prologue_back == 0 and p.prologue_verify == b"\x55\x8b\xec"


def test_player_signature_is_literal_no_wildcards():
    # The login signature is an exact AOB (no wildcard pattern), matched literally.
    assert HOOKS["player"].pattern is None
    assert sig.PLAYER_SIG == bytes.fromhex("558bec568bf1578b465885c0")
    assert sig.PLAYER_NAME_OFFSET == 24
    assert sig.PLAYER_SIBLING_OFFSET == 100
    assert sig.PLAYER_RELATIONSHIP_OFFSET == 119


def test_existing_specs_are_not_player_hooks():
    for name in ("dialogue", "quest", "walkthrough", "corner_text", "nameplates", "network_text"):
        assert HOOKS[name].player is False


# ---------------------------------------------------------------- PlayerHook.serve_once

STATE = 0x10
SLOT = 0x14
STRUCT = 0x4000


class FakeMem:
    """Address-space stub: u32 reads from a dict, byte buffers, and a write log."""

    def __init__(self) -> None:
        self.u32: dict[int, int] = {}
        self.buffers: dict[int, bytes] = {}
        self.writes: list[tuple[int, bytes]] = []

    def read_u32(self, addr: int) -> int:
        return self.u32.get(addr, 0)

    def read(self, addr: int, size: int) -> bytes:
        return self.buffers.get(addr, b"")[:size]

    def write(self, addr: int, data: bytes) -> None:
        self.writes.append((addr, bytes(data)))
        if addr == STATE:
            self.u32[STATE] = struct.unpack("<I", data[:4])[0]


def _hook() -> PlayerHook:
    return PlayerHook(func_addr=0x400000, cave_addr=0, state_addr=STATE, slot_addr=SLOT,
                      code_addr=0, saved_bytes=b"")


def _mem(player_ja: bytes, sibling_ja: bytes, relationship: int = 0x02) -> FakeMem:
    """Request-pending FakeMem with a login struct: STATE=REQUEST, SLOT=struct addr, names laid out
    at +24 / +100 and the relationship byte at +119."""
    m = FakeMem()
    m.u32[STATE] = STATE_REQUEST
    m.u32[SLOT] = STRUCT
    # Each field is its own keyed buffer at the absolute offset the hook reads from.
    m.buffers[STRUCT + sig.PLAYER_NAME_OFFSET] = player_ja + b"\x00" + b"junk"
    m.buffers[STRUCT + sig.PLAYER_SIBLING_OFFSET] = sibling_ja + b"\x00" + b"junk"
    m.buffers[STRUCT + sig.PLAYER_RELATIONSHIP_OFFSET] = bytes([relationship])
    return m


def _writes_to(mem: FakeMem, addr: int) -> list[bytes]:
    return [d for a, d in mem.writes if a == addr]


def test_serve_once_reads_names_and_calls_apply_names():
    mem = _mem("タイカン".encode(), "アンルシア".encode(), relationship=0x03)
    seen = {}

    def apply_names(player_ja, sibling_ja, relationship):
        seen["player"] = player_ja
        seen["sibling"] = sibling_ja
        seen["rel"] = relationship
        return ("Taikan", "Anlucia")  # truthy -> apply_names reports a real change

    out = _hook().serve_once(mem, apply_names)
    assert seen["player"] == "タイカン"
    assert seen["sibling"] == "アンルシア"
    assert seen["rel"] == 0x03
    assert out == "タイカン"  # returns JA player name for logging ONLY when apply_names changed


def test_serve_once_returns_none_when_apply_names_reports_no_change():
    # apply_names returning a falsy value (no change / idempotent) must surface as None from
    # serve_once so the caller's on_line/log does NOT fire on the unchanged repeats.
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    out = _hook().serve_once(mem, lambda p, s, r: None)
    assert out is None
    # STATE is still released even though nothing changed (the request was pending).
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]


def test_serve_once_releases_state_to_done():
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    _hook().serve_once(mem, lambda p, s, r: None)
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # login thread released


def test_serve_once_writes_only_the_state_release_NOTHING_into_game():
    """CRITICAL: the ONLY write the read-only hook makes is the STATE-release dword. It must NEVER
    write into the struct or any game buffer (a write there could corrupt/crash the game)."""
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    _hook().serve_once(mem, lambda p, s, r: None)
    # Exactly one write, and it is the STATE release. No write to the struct or its name fields.
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]
    assert _writes_to(mem, STRUCT) == []
    assert _writes_to(mem, STRUCT + sig.PLAYER_NAME_OFFSET) == []
    assert _writes_to(mem, STRUCT + sig.PLAYER_SIBLING_OFFSET) == []
    assert _writes_to(mem, SLOT) == []


def test_serve_once_releases_state_even_when_apply_names_raises():
    mem = _mem("タイカン".encode(), "アンルシア".encode())

    def boom(p, s, r):
        raise RuntimeError("apply exploded")

    _hook().serve_once(mem, boom)  # must NOT propagate
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # still released
    # Even on error, the ONLY game write is the STATE release.
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]


def test_serve_once_noop_when_not_requested():
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    mem.u32[STATE] = STATE_DONE  # no pending login request
    called = {"n": 0}

    def apply_names(p, s, r):
        called["n"] += 1

    assert _hook().serve_once(mem, apply_names) is None
    assert called["n"] == 0
    assert mem.writes == []  # not requested -> no read, no STATE write at all


def test_serve_once_skips_apply_when_player_name_empty():
    # An empty player name (struct not yet populated) must not trigger apply_names, but STILL release.
    mem = _mem(b"", b"", relationship=0x00)
    called = {"n": 0}
    out = _hook().serve_once(mem, lambda p, s, r: called.__setitem__("n", called["n"] + 1))
    assert called["n"] == 0
    assert out is None
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # released regardless


def test_serve_once_caps_the_name_read_and_stops_at_nul():
    # The name read is capped and NUL-terminated; trailing junk after the NUL is not part of the name.
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    seen = {}
    _hook().serve_once(mem, lambda p, s, r: seen.update(player=p, sibling=s) or True)
    assert "junk" not in seen["player"] and "junk" not in seen["sibling"]


def test_serve_once_empty_name_path_writes_only_state_no_garbage():
    # Empty player name: apply_names must NOT run, serve_once returns None, and the ONLY write is the
    # STATE release (no garbage name stored, no other game write).
    mem = _mem(b"", b"", relationship=0x00)
    called = {"n": 0}
    out = _hook().serve_once(mem, lambda p, s, r: called.__setitem__("n", called["n"] + 1) or True)
    assert called["n"] == 0  # apply_names not called on the empty-name path
    assert out is None
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]  # only the state write


def test_serve_once_no_change_path_writes_only_state():
    # apply_names runs but reports no change (returns None / idempotent): serve_once returns None and
    # the ONLY write is the STATE release.
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    out = _hook().serve_once(mem, lambda p, s, r: None)  # no change
    assert out is None
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]  # only the state write


def test_serve_once_returns_none_when_sibling_read_raises_but_releases_state():
    # If a read raises AFTER the player name was read (e.g. the sibling read), serve_once must NOT
    # report success (returns None) yet STILL release STATE. Guards the explicit `applied` flag.
    mem = _mem("タイカン".encode(), "アンルシア".encode())

    boom = {"armed": False}
    real_read = mem.read

    def exploding_read(addr, size):
        # Let the player-name read succeed, then blow up on the sibling-name read.
        if addr == STRUCT + sig.PLAYER_SIBLING_OFFSET:
            raise RuntimeError("sibling read exploded")
        return real_read(addr, size)

    mem.read = exploding_read
    applied = {"n": 0}
    out = _hook().serve_once(mem, lambda p, s, r: applied.__setitem__("n", applied["n"] + 1) or True)
    assert out is None  # a post-read error is not a success
    assert applied["n"] == 0  # apply_names never ran (sibling read raised first)
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # STATE still released
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]  # only the state write


def test_serve_once_relationship_missing_passes_zero_and_releases_state():
    # FakeMem doesn't populate the relationship field -> mem.read returns b'' -> relationship=0 is
    # passed to apply_names, and STATE is still released.
    mem = _mem("タイカン".encode(), "アンルシア".encode())
    del mem.buffers[STRUCT + sig.PLAYER_RELATIONSHIP_OFFSET]  # field absent -> read returns b''
    seen = {}

    def apply_names(p, s, r):
        seen["rel"] = r
        return ("Taikan", "Anlucia")

    _hook().serve_once(mem, apply_names)
    assert seen["rel"] == 0  # empty relationship read -> 0, not an error
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # STATE released


def test_read_cstring_no_interior_nul_returns_empty_garbage_guard():
    # A >64-byte buffer with NO interior NUL is garbage (a real name terminates well within 64B):
    # _read_cstring returns "" rather than 64 raw bytes that would be stored as a garbage name.
    mem = FakeMem()
    mem.buffers[STRUCT] = b"A" * 200  # no NUL anywhere in the cap window
    assert PlayerHook._read_cstring(mem, STRUCT) == ""


def test_serve_once_no_interior_nul_player_skips_apply_and_releases():
    # End-to-end: a player-name field with no interior NUL -> _read_cstring returns "" -> treated as
    # empty -> apply_names NOT called, no garbage stored, STATE still released.
    mem = FakeMem()
    mem.u32[STATE] = STATE_REQUEST
    mem.u32[SLOT] = STRUCT
    mem.buffers[STRUCT + sig.PLAYER_NAME_OFFSET] = b"A" * 200  # no NUL in the cap window
    called = {"n": 0}
    out = _hook().serve_once(mem, lambda p, s, r: called.__setitem__("n", called["n"] + 1) or True)
    assert called["n"] == 0  # garbage name -> apply_names not called
    assert out is None
    assert mem.writes == [(STATE, struct.pack("<I", STATE_DONE))]  # only the state write


# ---------------------------------------------------------------- build_apply_names

def _cfg():
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=0,
    )
    return SimpleNamespace(translate=tr)


def test_build_apply_names_updates_live_translator(tmp_path):
    from dqxclarity.translate.romanize import is_available

    cache = TranslationCache(tmp_path / "p.db")
    t = Translator(cache)
    cfg = _cfg()
    saves = []
    apply_names = build_apply_names(cfg, t, save=saves.append)

    apply_names("たろう", "はなこ", 0x02)
    # Live translator names updated (JA always; EN romanized when available, else falls back to JA).
    assert t.player_name_ja == "たろう"
    assert t.sibling_name_ja == "はなこ"
    assert t.sibling_relationship == 0x02
    if is_available():
        assert t.player_name_en and t.player_name_en.lower().startswith("tar")
        assert t.sibling_name_en
    else:
        assert t.player_name_en == "たろう"  # no romanizer -> keep JA, never blank


def test_build_apply_names_saves_config(tmp_path):
    cache = TranslationCache(tmp_path / "p2.db")
    t = Translator(cache)
    cfg = _cfg()
    saved = []
    apply_names = build_apply_names(cfg, t, save=lambda c: saved.append(c))

    apply_names("たろう", "はなこ", 0x01)
    assert saved == [cfg]  # persisted once
    assert cfg.translate.player_name_ja == "たろう"
    assert cfg.translate.sibling_name_ja == "はなこ"


def test_build_apply_names_idempotent_no_redundant_save(tmp_path):
    cache = TranslationCache(tmp_path / "p3.db")
    t = Translator(cache)
    cfg = _cfg()
    saves = []
    apply_names = build_apply_names(cfg, t, save=saves.append)

    apply_names("たろう", "はなこ", 0x02)
    apply_names("たろう", "はなこ", 0x02)  # unchanged -> must NOT re-save
    apply_names("たろう", "はなこ", 0x02)
    assert len(saves) == 1  # saved exactly once for the unchanged repeats


def test_build_apply_names_resaves_on_change(tmp_path):
    cache = TranslationCache(tmp_path / "p4.db")
    t = Translator(cache)
    cfg = _cfg()
    saves = []
    apply_names = build_apply_names(cfg, t, save=saves.append)

    apply_names("たろう", "はなこ", 0x02)
    apply_names("じろう", "はなこ", 0x02)  # player changed -> re-save
    assert len(saves) == 2
    assert t.player_name_ja == "じろう"


def test_build_apply_names_returns_resolved_en(tmp_path):
    from dqxclarity.translate.romanize import is_available

    cache = TranslationCache(tmp_path / "p5.db")
    t = Translator(cache)
    cfg = _cfg()
    apply_names = build_apply_names(cfg, t, save=lambda c: None)
    pen, sen = apply_names("たろう", "はなこ", 0x02)
    if is_available():
        assert pen.lower().startswith("tar") and sen
    else:
        assert pen == "たろう" and sen == "はなこ"


def test_serve_once_signals_change_once_then_idempotent(tmp_path):
    # End-to-end with the REAL build_apply_names: two consecutive logins with the SAME names ->
    # first serve_once returns the player JA (changed, so on_line/print would fire), the second
    # returns None (idempotent, no fire), and config.save is called exactly once.
    cache = TranslationCache(tmp_path / "once.db")
    t = Translator(cache)
    cfg = _cfg()
    saves = []
    apply_names = build_apply_names(cfg, t, save=saves.append)

    mem1 = _mem("たろう".encode(), "はなこ".encode())
    out1 = _hook().serve_once(mem1, apply_names)
    assert out1 == "たろう"  # real change -> caller logs this one

    mem2 = _mem("たろう".encode(), "はなこ".encode())
    out2 = _hook().serve_once(mem2, apply_names)
    assert out2 is None  # unchanged -> no log/print on the repeat

    assert len(saves) == 1  # saved exactly once across both logins
    # Each login still released its own STATE dword (and wrote nothing else into the game).
    assert mem1.writes == [(STATE, struct.pack("<I", STATE_DONE))]
    assert mem2.writes == [(STATE, struct.pack("<I", STATE_DONE))]


def test_build_apply_names_idempotent_across_relationship_change(tmp_path):
    # relationship is NOT part of idempotency (it isn't persisted): the SAME names with a DIFFERENT
    # relationship must NOT re-save or re-signal a change, but the relationship is still captured.
    cache = TranslationCache(tmp_path / "rel.db")
    t = Translator(cache)
    cfg = _cfg()
    saves = []
    apply_names = build_apply_names(cfg, t, save=saves.append)

    assert apply_names("たろう", "はなこ", 0x02) is not None  # first time -> change
    assert apply_names("たろう", "はなこ", 0x05) is None       # names same -> no change, no save
    assert len(saves) == 1
    assert t.sibling_relationship == 0x05  # still captured for future use


def test_build_apply_names_default_save_is_config_save(monkeypatch, tmp_path):
    # When no save fn is passed, apply_names persists via config.save (monkeypatched here).
    import dqxclarity.config as config_mod

    saved = []
    monkeypatch.setattr(config_mod, "save", lambda c: saved.append(c))
    cache = TranslationCache(tmp_path / "p6.db")
    t = Translator(cache)
    cfg = _cfg()
    apply_names = build_apply_names(cfg, t)  # no save= -> defaults to config.save
    apply_names("たろう", "はなこ", 0x02)
    assert saved == [cfg]


# ---------------------------------------------------------------- LIVE name update (no restart)

def test_community_lookup_picks_up_name_after_apply_names(tmp_path):
    """A community_lookup built BEFORE apply_names runs must use the NEW player name AFTER it runs —
    proving the dynamic read off the translator, not a stale build-time closure over cfg."""
    cache = TranslationCache(tmp_path / "live.db")
    # Curated line stores the player name as a placeholder; matches for any player.
    cache.store("<pnplacehold>さん、こんにちは。", "Hello, <pnplacehold>.", "community")
    t = Translator(cache)
    cfg = _cfg()

    # Build the lookup BEFORE any name is known (translator names are empty).
    lookup = _make_community_lookup(cfg, t)
    # Before detection: the in-game JA has the literal player name, which can't be placeholdered yet.
    assert lookup("たろうさん、こんにちは。") is None

    # PLAYER hook fires -> apply_names updates the LIVE translator names.
    apply_names = build_apply_names(cfg, t, save=lambda c: None)
    apply_names("たろう", "はなこ", 0x02)

    # The SAME lookup (built before) now resolves: the literal name is swapped to the placeholder for
    # the lookup key and the EN name swapped back into the result — all without rebuilding the fn.
    out = lookup("たろうさん、こんにちは。")
    assert out is not None
    assert "<pnplacehold>" not in out
    assert t.player_name_en in out  # the resolved EN name was substituted back in


def test_translator_seeds_names_default_empty(tmp_path):
    # A fresh translator has empty live names (seeded later from cfg / by the player hook).
    cache = TranslationCache(tmp_path / "seed.db")
    t = Translator(cache)
    assert t.player_name_ja == "" and t.player_name_en == ""
    assert t.sibling_name_ja == "" and t.sibling_name_en == ""
    assert t.sibling_relationship == 0


# ---------------------------------------------------------------- serve() integration (mixed hooks)

class FakeBlockingHook:
    """Minimal text hook for the serve loop: serves one ja field via its translate_fn, then idles.

    Mirrors the BlockingHook serve_once contract serve() relies on: ``serve_once(mem, fn)`` returns a
    non-None value (here the translated text) once, then None thereafter. It writes the translated
    text back into game memory the FIRST time, so the test can prove the PlayerHook (read-only) wrote
    ONLY the STATE dword while the text hook DID write into game memory.
    """

    def __init__(self, ja: str, write_addr: int) -> None:
        self.ja = ja
        self.write_addr = write_addr
        self._served = False

    def serve_once(self, mem, translate_fn):
        if self._served:
            return None
        self._served = True
        out = translate_fn(self.ja)
        mem.write(self.write_addr, (out or "").encode("utf-8"))  # text hook DOES write game memory
        return self.ja

    def restore(self, mem):  # pragma: no cover - not exercised by serve()
        pass


TEXT_WRITE_ADDR = 0x9000


def test_serve_mixes_player_hook_and_blocking_hook():
    """serve() polls a PlayerHook alongside a BlockingHook. The 3-arg apply_names is called with the
    right args, STATE is released, and the PlayerHook wrote ONLY the STATE dword into game memory
    (while the text hook DID write its translation) — proving the read-only invariant under serve()."""
    mem = _mem("タイカン".encode(), "アンルシア".encode(), relationship=0x03)

    seen = {}

    def apply_names(player_ja, sibling_ja, relationship):
        seen["args"] = (player_ja, sibling_ja, relationship)
        return ("Taikan", "Anlucia")  # truthy -> real change -> serve_once surfaces it

    player = _hook()
    text = FakeBlockingHook("こんにちは", TEXT_WRITE_ADDR)

    lines: list[tuple[str, str]] = []
    stop = threading.Event()

    def on_line(name, value):
        lines.append((name, value))
        # Stop once BOTH hooks have reported their one-shot field, so serve() returns promptly.
        if len(lines) >= 2:
            stop.set()

    hooks = [
        ("player", player, apply_names),
        ("dialogue", text, lambda ja: "Hello"),
    ]
    served = serve(mem, hooks, stop=stop, on_line=on_line)

    assert served >= 2
    assert seen["args"] == ("タイカン", "アンルシア", 0x03)  # 3-arg apply_names, right values
    # PlayerHook is read-only: STATE released, and the ONLY player-side write is the STATE dword.
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)
    assert _writes_to(mem, STATE) == [struct.pack("<I", STATE_DONE)]
    assert _writes_to(mem, STRUCT) == []
    assert _writes_to(mem, STRUCT + sig.PLAYER_NAME_OFFSET) == []
    assert _writes_to(mem, STRUCT + sig.PLAYER_SIBLING_OFFSET) == []
    # The text hook DID write its translation -> proves the no-write assertion is specific to the
    # player hook, not an artifact of nothing writing at all.
    assert _writes_to(mem, TEXT_WRITE_ADDR) == ["Hello".encode("utf-8")]
    # serve() surfaced both hooks' fields exactly once each.
    assert ("player", "タイカン") in lines
    assert ("dialogue", "こんにちは") in lines


def test_build_apply_names_empty_read_does_not_clobber_known_name(tmp_path):
    # A blank/garbage hook read must NEVER overwrite a known name (the mid-session "Squid" regression):
    # an empty player_ja keeps the current pinned/detected value instead of clearing it.
    cache = TranslationCache(tmp_path / "p_guard.db")
    t = Translator(cache)
    cfg = _cfg()
    saves = []
    apply_names = build_apply_names(cfg, t, save=saves.append)

    apply_names("たろう", "はなこ", 0x01)        # a real detection sets the names
    assert t.player_name_ja == "たろう"
    pen, sen = t.player_name_en, t.sibling_name_en
    saves.clear()

    result = apply_names("", "", 0x00)           # a blank read must NOT clear them
    assert t.player_name_ja == "たろう"          # kept
    assert t.sibling_name_ja == "はなこ"
    assert t.player_name_en == pen and t.sibling_name_en == sen
    assert result is None and saves == []        # no change -> no spurious save

    # A real player read with a still-blank sibling keeps the existing sibling.
    apply_names("じろう", "", 0x00)
    assert t.player_name_ja == "じろう"           # updated
    assert t.sibling_name_ja == "はなこ"          # blank sibling preserved
