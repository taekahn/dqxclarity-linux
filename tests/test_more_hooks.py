"""Tests for the two ported text hooks: nameplates (overhead names) and corner_text (top-right).

These are mechanically identical to the existing dialogue/walkthrough prologue hooks: a blocking
detour captures a text/name pointer, translates, and writes back. The new pieces under test:
  * both specs resolve via ``find_function`` (match IS the function; prologue 55 8B EC verified);
  * corner_text captures the 3rd stack arg (CAPTURE_ARG2 = 8B 44 24 30), nameplates the 1st
    (CAPTURE_ARG0 = 8B 44 24 28);
  * the blocking shellcode splices CAPTURE_ARG2 right after pushad+pushfd, jmp-back unchanged;
  * the NAME translate path (build_name_translate_fn): cache hit -> EN; uncached JA -> romaji;
    non-JA -> None;
  * the spec profiles (is_name, stolen_len, capture, pattern) match the directive.
"""

from __future__ import annotations

import struct

from types import SimpleNamespace

import pytest

from dqxclarity.process import detour
from dqxclarity.process import signatures as sig
from dqxclarity.process.hooks import HOOKS, HookSpec, find_function
from dqxclarity.runtime.dispatch import build_name_translate_fn
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.romanize import is_available


# --------------------------------------------------------------------- find_function resolution


class FakeScanMem:
    """Stubs pattern_scan (preset matches) and read (preset prologue bytes). Mirrors test_hooks."""

    def __init__(self, matches: list[int], prologue: dict[int, bytes]) -> None:
        self._matches = matches
        self._prologue = prologue

    def pattern_scan(self, pattern, *, data_only=False, limit=4):  # noqa: ARG002
        return self._matches

    def read(self, addr: int, size: int) -> bytes:
        return self._prologue.get(addr, b"")[:size]


def test_corner_text_find_function_match_is_func_and_prologue_verifies():
    # prologue_back=0 -> the match IS the function; prologue 55 8B EC must verify.
    c = HOOKS["corner_text"]
    assert c.prologue_back == 0 and c.prologue_verify == b"\x55\x8b\xec"
    match = 0x00700000
    mem = FakeScanMem([match], {match: b"\x55\x8b\xec"})
    assert find_function(mem, c) == match


def test_nameplates_find_function_match_is_func_and_prologue_verifies():
    n = HOOKS["nameplates"]
    assert n.prologue_back == 0 and n.prologue_verify == b"\x55\x8b\xec"
    match = 0x00800000
    mem = FakeScanMem([match], {match: b"\x55\x8b\xec"})
    assert find_function(mem, n) == match


def test_corner_text_find_function_rejects_wrong_prologue():
    match = 0x00700000
    mem = FakeScanMem([match], {match: b"\x90\x90\x90"})  # not 55 8b ec
    assert find_function(mem, HOOKS["corner_text"]) is None


def test_nameplates_find_function_rejects_wrong_prologue():
    match = 0x00800000
    mem = FakeScanMem([match], {match: b"\x90\x90\x90"})  # not 55 8b ec
    assert find_function(mem, HOOKS["nameplates"]) is None


# ------------------------------------------------------------------------- capture-arg selection


def test_capture_arg2_constant_bytes():
    assert detour.CAPTURE_ARG2 == b"\x8b\x44\x24\x30"  # mov eax,[esp+0x30]


def test_corner_text_uses_capture_arg2():
    assert HOOKS["corner_text"].capture == detour.CAPTURE_ARG2 == b"\x8b\x44\x24\x30"


def test_nameplates_uses_capture_arg0():
    assert HOOKS["nameplates"].capture == detour.CAPTURE_ARG0 == b"\x8b\x44\x24\x28"


# --------------------------------------------------------------- blocking shellcode with CAPTURE_ARG2


def _decode_rel32(buf: bytes, e9_off: int) -> int:
    rel = struct.unpack("<i", buf[e9_off + 1 : e9_off + 5])[0]
    return e9_off + 5 + rel  # target relative to buf base


def test_blocking_shellcode_capture_arg2_spliced_and_jumpback_recomputed():
    # Mirror test_blocking_shellcode_custom_capture_threaded_and_jumpback_recomputed for ARG2.
    cap = detour.CAPTURE_ARG2
    func = 0x00700000
    stolen = bytes.fromhex("558bec8b4510")  # corner_text prologue: 6 bytes (whole instructions)
    code_addr = 0x02060010
    sc = detour.build_blocking_shellcode(
        code_addr=code_addr, state_addr=0x02060000, slot_addr=0x02060004,
        func_addr=func, stolen=stolen, capture=cap,
    )
    assert sc[0] == 0x60 and sc[1] == 0x9C            # pushad; pushfd
    assert sc[2 : 2 + len(cap)] == cap                # 8b 44 24 30 spliced right after 60 9c
    assert stolen in sc                               # original prologue preserved
    assert b"\xf3\x90" in sc                          # spin loop intact
    assert b"\x74\x05" in sc and b"\x75\xf2" in sc    # self-relative jumps unchanged
    # jmp-back still lands at func + len(stolen)
    e9_off = len(sc) - 5
    assert sc[e9_off] == 0xE9
    assert code_addr + _decode_rel32(sc, e9_off) == func + len(stolen)


# ------------------------------------------------------------------------ build_name_translate_fn


def _cfg(**over):
    """Minimal config stub: the name path only reads cfg.translate.* for the placeholder swap."""
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=3,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


def test_name_translate_fn_returns_cached_en(tmp_path):
    c = TranslationCache(tmp_path / "names.db")
    c.store("スライム", "Slime", "community")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t)
    assert fn("スライム") == "Slime"  # community/cache hit wins over romanization


def test_name_translate_fn_non_japanese_returns_none(tmp_path):
    c = TranslationCache(tmp_path / "names2.db")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t)
    assert fn("Bob") is None  # not Japanese -> leave as-is


def test_name_translate_fn_uncached_japanese_romanizes(tmp_path):
    if not is_available():
        pytest.skip("pykakasi unavailable")
    c = TranslationCache(tmp_path / "names3.db")
    t = Translator(c)
    fn = build_name_translate_fn(_cfg(), t)
    out = fn("たろう")  # uncached player-style name -> transliterated
    assert out is not None
    assert out.lower().startswith("tar")  # romaji of たろう (Tarou)


# ------------------------------------------------------------------------------ the spec profiles


def test_corner_text_hookspec_profile():
    c = HOOKS["corner_text"]
    assert isinstance(c, HookSpec)
    assert c.is_name is False                                   # ordinary NPC text -> regular path
    assert c.stolen_len == sig.CORNER_TEXT_STOLEN_LEN == 6
    assert c.capture == detour.CAPTURE_ARG2
    assert c.pattern == sig.CORNER_TEXT_PATTERN
    assert c.signature == b""                                   # matched via wildcard pattern
    assert c.prologue_back == 0 and c.prologue_verify == b"\x55\x8b\xec"
    assert c.wrap_width == 46 and c.lines_per_page == 0 and c.sync is True


def test_nameplates_hookspec_profile():
    n = HOOKS["nameplates"]
    assert isinstance(n, HookSpec)
    assert n.is_name is True                                    # NAME -> name translate path
    assert n.stolen_len == sig.NAMEPLATES_STOLEN_LEN == 10
    assert n.capture == detour.CAPTURE_ARG0
    assert n.pattern == sig.NAMEPLATES_PATTERN
    assert n.signature == b""                                   # matched via wildcard pattern
    assert n.prologue_back == 0 and n.prologue_verify == b"\x55\x8b\xec"
    assert n.wrap_width == 46 and n.lines_per_page == 0 and n.sync is True


def test_existing_specs_are_not_names():
    # is_name defaults False; only nameplates flips it. Guards the dialogue/quest/walkthrough path.
    for name in ("dialogue", "quest", "walkthrough", "corner_text"):
        assert HOOKS[name].is_name is False
