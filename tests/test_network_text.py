"""Tests for the network_text RETURN hook: a return-site detour with a shadow stack that reads the
function's result AFTER it returns true (mirrors upstream Frida Interceptor.attach({onLeave})).

Covers (offline — no game, no pytest-on-target needed):
  * build_return_shellcode entry/exit structure, the retaddr-hijack, the jmp-back, the spin loop,
    the jne-.skip displacement summing to 44, and exit esp balance (push/pop count);
  * find_function resolving NETWORK_TEXT_PATTERN with prologue_verify 55 8b ec;
  * ReturnHook.serve_once: translate fn called with (ja, category), EN written within `length`,
    oversize EN skipped, STATE always released to DONE;
  * build_network_translate_fn category routing (name vs text vs non-JA).
"""

from __future__ import annotations

import struct

from types import SimpleNamespace

from dqxclarity.process import detour
from dqxclarity.process import signatures as sig
from dqxclarity.process.detour import (
    STATE_DONE,
    STATE_REQUEST,
    ReturnHook,
    build_return_shellcode,
)
from dqxclarity.process.hooks import HOOKS, HookSpec, find_function
from dqxclarity.runtime.dispatch import (
    NAME_TAGS,
    NET_GENERIC_CATEGORIES,
    NET_IGNORE_CATEGORIES,
    NET_NAME_CATEGORIES,
    NET_TRANSLATE_CATEGORIES,
    _is_name_category,
    build_network_translate_fn,
)
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.pipeline import Translator


# ----------------------------------------------------------------------- build_return_shellcode

# Concrete cave layout used by the assembler tests (mirrors install_return_hook).
CAVE = 0x02060000
STATE_ADDR = CAVE + 0
CTX_SLOT = CAVE + 4
SP_ADDR = CAVE + 8
SHADOW = CAVE + 12
ENTRY_CODE = CAVE + 12 + detour.SHADOW_BYTES
FUNC = 0x00500000
STOLEN = bytes.fromhex("558bec81ecdc030000")  # 55 | 8B EC | 81 EC DC030000 = 9 bytes


def _decode_rel32(buf: bytes, e9_off: int) -> int:
    rel = struct.unpack("<i", buf[e9_off + 1 : e9_off + 5])[0]
    return e9_off + 5 + rel  # target relative to buf base


def _build():
    # measure entry length, then real exit_code (as install_return_hook does)
    entry0, _ = build_return_shellcode(
        entry_code=ENTRY_CODE, exit_code=0, state_addr=STATE_ADDR, ctx_slot=CTX_SLOT,
        sp_addr=SP_ADDR, shadow=SHADOW, func_addr=FUNC, stolen=STOLEN,
    )
    exit_code = ENTRY_CODE + len(entry0)
    return build_return_shellcode(
        entry_code=ENTRY_CODE, exit_code=exit_code, state_addr=STATE_ADDR, ctx_slot=CTX_SLOT,
        sp_addr=SP_ADDR, shadow=SHADOW, func_addr=FUNC, stolen=STOLEN,
    ), exit_code


def test_entry_starts_pushad_pushfd():
    (entry, _), _ = _build()
    assert entry[0] == 0x60 and entry[1] == 0x9C  # pushad; pushfd


def test_entry_hijacks_return_address_with_exit_code():
    (entry, _), exit_code = _build()
    # mov dword[esp+0x24], exit_code  =  C7 44 24 24 <imm32>
    needle = b"\xc7\x44\x24\x24" + struct.pack("<I", exit_code)
    assert needle in entry


def test_entry_preserves_stolen_and_jumps_back():
    (entry, _), _ = _build()
    assert STOLEN in entry  # original prologue preserved
    e9_off = len(entry) - 5
    assert entry[e9_off] == 0xE9
    assert ENTRY_CODE + _decode_rel32(entry, e9_off) == FUNC + len(STOLEN)  # jmp back to func+stolen


def test_entry_pushes_shadow_frame_with_absolute_disps():
    (entry, _), _ = _build()
    # mov [ecx+shadow],edx  =  89 91 <shadow>   ; frame.retaddr
    assert b"\x89\x91" + struct.pack("<I", SHADOW) in entry
    # mov [ecx+shadow+4],edx  =  89 91 <shadow+4> ; frame.ctx
    assert b"\x89\x91" + struct.pack("<I", SHADOW + 4) in entry
    # reads orig retaddr at [esp+0x24] and ctx a1 at [esp+0x28] (after pushad+pushfd)
    assert b"\x8b\x54\x24\x24" in entry  # mov edx,[esp+0x24]
    assert b"\x8b\x54\x24\x28" in entry  # mov edx,[esp+0x28]


def test_exit_starts_push_eax_pushfd_and_ends_return_sequence():
    (_, exit_sc), _ = _build()
    assert exit_sc[0] == 0x50 and exit_sc[1] == 0x9C  # push eax; pushfd
    assert exit_sc[-5:] == b"\x5a\x9d\x58\xff\xe2"     # pop edx; popfd; pop eax; jmp edx


def test_exit_contains_spin_loop_and_state_signal():
    (_, exit_sc), _ = _build()
    assert b"\xf3\x90" in exit_sc                       # pause
    assert b"\x74\x05" in exit_sc and b"\x75\xf2" in exit_sc  # je .done +5 / jnz .wait -14
    assert b"\xa3" + struct.pack("<I", CTX_SLOT) in exit_sc   # mov [ctx_slot],eax
    # mov dword[STATE],1 (request)
    assert b"\xc7\x05" + struct.pack("<I", STATE_ADDR) + b"\x01\x00\x00\x00" in exit_sc
    # mov dword[STATE],0 (idle/release on the spin path)
    assert b"\xc7\x05" + struct.pack("<I", STATE_ADDR) + b"\x00\x00\x00\x00" in exit_sc


def test_exit_jne_skip_displacement_is_44():
    (_, exit_sc), _ = _build()
    # find the cmp ecx,1 (83 F9 01) then the jne (75 xx) right after it
    i = exit_sc.find(b"\x83\xf9\x01")
    assert i != -1
    jne = i + 3
    assert exit_sc[jne] == 0x75  # jne
    disp = exit_sc[jne + 1]
    assert disp == 44 == 0x2C  # documented displacement to .skip (pop edx)
    # and it lands exactly on the 5A (pop edx)
    target = jne + 2 + disp
    assert exit_sc[target] == 0x5A


def test_exit_esp_balance_push_pop_counts_match():
    """I1: push eax, pushfd, push edx (-12) then pop edx, popfd, pop eax (+12) -> net 0 at jmp edx."""
    (_, exit_sc), _ = _build()
    # the explicit single-byte stack ops in the exit block: 50(push eax) 9C(pushfd) 52(push edx)
    # balanced by 5A(pop edx) 9D(popfd) 58(pop eax). Count them.
    pushes = exit_sc.count(b"\x50") + exit_sc.count(b"\x9c") + exit_sc.count(b"\x52")
    pops = exit_sc.count(b"\x5a") + exit_sc.count(b"\x9d") + exit_sc.count(b"\x58")
    # exactly one of each push and its matching pop (no stray duplicates in this block)
    assert exit_sc.count(b"\x50") == 1 and exit_sc.count(b"\x52") == 1 and exit_sc.count(b"\x9c") == 1
    assert exit_sc.count(b"\x5a") == 1 and exit_sc.count(b"\x58") == 1 and exit_sc.count(b"\x9d") == 1
    assert pushes == pops == 3  # net esp change 0


# --------------------------------------------------------------------------- find_function resolve


class FakeScanMem:
    """Stubs pattern_scan (preset matches) and read (preset prologue bytes). Mirrors test_hooks."""

    def __init__(self, matches, prologue) -> None:
        self._matches = matches
        self._prologue = prologue

    def pattern_scan(self, pattern, *, data_only=False, limit=4):  # noqa: ARG002
        return self._matches

    def read(self, addr: int, size: int) -> bytes:
        return self._prologue.get(addr, b"")[:size]


def test_network_text_find_function_match_is_func_and_prologue_verifies():
    n = HOOKS["network_text"]
    assert n.prologue_back == 0 and n.prologue_verify == b"\x55\x8b\xec"
    match = 0x00900000
    mem = FakeScanMem([match], {match: b"\x55\x8b\xec"})
    assert find_function(mem, n) == match


def test_network_text_find_function_rejects_wrong_prologue():
    match = 0x00900000
    mem = FakeScanMem([match], {match: b"\x90\x90\x90"})  # not 55 8b ec
    assert find_function(mem, HOOKS["network_text"]) is None


def test_network_text_hookspec_profile():
    n = HOOKS["network_text"]
    assert isinstance(n, HookSpec)
    assert n.return_hook is True
    assert n.is_name is False
    assert n.stolen_len == sig.NETWORK_TEXT_STOLEN_LEN == 9
    assert n.pattern == sig.NETWORK_TEXT_PATTERN
    assert n.signature == b""
    assert n.prologue_back == 0 and n.prologue_verify == b"\x55\x8b\xec"
    assert n.wrap_width == 46 and n.lines_per_page == 0 and n.sync is True


def test_existing_specs_are_not_return_hooks():
    for name in ("dialogue", "quest", "walkthrough", "corner_text", "nameplates"):
        assert HOOKS[name].return_hook is False


# ----------------------------------------------------------------------- ReturnHook.serve_once

STATE = 0x10
CTX = 0x14
CTXBASE = 0x4000
STARTBUF = 0x5000
CATBUF = 0x6000


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


def _hook() -> ReturnHook:
    return ReturnHook(func_addr=0x400000, cave=0, state_addr=STATE, ctx_slot=CTX, saved_bytes=b"")


def _mem(ja: bytes, length: int | None, category: bytes = b"<%sM_kaisetubun>") -> FakeMem:
    """Build a request-pending FakeMem: STATE=REQUEST, ctx fields set, JA at start, category string."""
    m = FakeMem()
    m.u32[STATE] = STATE_REQUEST
    m.u32[CTX] = CTXBASE
    n = len(ja) if length is None else length
    end = STARTBUF + n  # start = end - length = STARTBUF
    m.u32[CTXBASE + 0x10] = n          # length
    m.u32[CTXBASE + 0x18] = end        # end of buffer
    m.u32[CTXBASE + 0x1C] = CATBUF     # category ptr
    m.buffers[STARTBUF] = ja + b"\x00" * 256  # buffer slack after the (non-terminated) JA span
    m.buffers[CATBUF] = category + b"\x00"
    return m


def _writes_to(mem: FakeMem, addr: int) -> list[bytes]:
    return [d for a, d in mem.writes if a == addr]


def test_serve_once_passes_ja_and_category_and_writes_en():
    ja = "むかしむかし".encode()  # 18 bytes
    mem = _mem(ja, None, category=b"<%sM_kaisetubun>")
    seen = {}

    def translate(j, cat):
        seen["ja"], seen["cat"] = j, cat
        return "Once upon a time"

    out = _hook().serve_once(mem, translate)
    assert seen["ja"] == "むかしむかし"
    assert seen["cat"] == "<%sM_kaisetubun>"
    w = _writes_to(mem, STARTBUF)
    assert w and w[-1].startswith(b"Once upon a time\x00")
    assert out == "むかしむかし"  # returns JA for logging
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # released


def test_serve_once_counts_a_serviced_request_but_not_an_idle_poll():
    # The hot-hook profiler counts a request ONLY when STATE_REQUEST was pending (a real game call),
    # never on an idle poll — so req/s reflects the game's true call rate on the hooked function.
    hook = _hook()
    hook.serve_once(_mem("x".encode(), None), lambda j, c: "Hi")  # STATE_REQUEST pending
    assert hook.requests == 1
    idle = FakeMem()
    idle.u32[STATE] = 0  # STATE_IDLE: no pending request
    assert hook.serve_once(idle, lambda j, c: "Hi") is None
    assert hook.requests == 1  # unchanged — an idle poll is not a serviced request


def test_serve_once_writes_within_length_and_zero_pads():
    ja = "テストです".encode()  # 15 bytes; EN shorter -> tail must be zeroed within `length`
    mem = _mem(ja, None)
    _hook().serve_once(mem, lambda j, c: "Hi")
    data = _writes_to(mem, STARTBUF)[-1]
    assert data.startswith(b"Hi\x00")
    assert len(data) == 15  # exactly `length` bytes (zero-padded over the old JA span)
    assert set(data[3:]) == {0}


def test_serve_once_skips_oversize_en():
    ja = "あ".encode()  # length = 3
    mem = _mem(ja, None)
    _hook().serve_once(mem, lambda j, c: "way too long to fit in three bytes")
    assert _writes_to(mem, STARTBUF) == []  # I1: never exceed the buffer span -> leave JA
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # still released


def test_serve_once_skips_when_length_exceeds_read_window():
    # I1: `length` is read from game memory. A corrupt/huge value must NOT drive a huge zero-pad
    # allocation or a write past the real buffer — strings longer than the read window are left
    # untranslated rather than partially corrupted.
    from dqxclarity.process.detour import _READ_WINDOW

    huge = _READ_WINDOW + 5000
    mem = _mem("あ".encode(), length=huge)
    _hook().serve_once(mem, lambda j, c: "short EN")
    assert _writes_to(mem, STARTBUF) == []  # nothing written for an over-window length
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # still released


def test_serve_once_releases_even_when_translate_raises():
    ja = "テスト".encode()
    mem = _mem(ja, None)

    def boom(j, c):
        raise RuntimeError("provider exploded")

    _hook().serve_once(mem, boom)  # must NOT propagate
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # released (I1/I4)
    assert _writes_to(mem, STARTBUF) == []  # nothing written on failure


def test_serve_once_noop_when_not_requested():
    mem = _mem("テスト".encode(), None)
    mem.u32[STATE] = STATE_DONE  # no pending request
    assert _hook().serve_once(mem, lambda j, c: "x") is None
    assert mem.writes == []


def test_serve_once_does_not_write_when_unchanged():
    mem = _mem("テスト".encode(), None)
    _hook().serve_once(mem, lambda j, c: j)  # translate returns JA unchanged
    assert _writes_to(mem, STARTBUF) == []
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)


def test_serve_once_uses_explicit_length_not_nul():
    # The buffer is NOT null-terminated at the JA end; serve_once must use the explicit length.
    ja = "あい".encode()  # 6 bytes
    mem = FakeMem()
    mem.u32[STATE] = STATE_REQUEST
    mem.u32[CTX] = CTXBASE
    mem.u32[CTXBASE + 0x10] = 6
    mem.u32[CTXBASE + 0x18] = STARTBUF + 6
    mem.u32[CTXBASE + 0x1C] = CATBUF
    # NO null after the JA span — trailing garbage instead; length must bound the decode/read.
    mem.buffers[STARTBUF] = ja + b"garbage trailing bytes that are not part of the string"
    mem.buffers[CATBUF] = b"<%sM_kaisetubun>\x00"
    seen = {}
    _hook().serve_once(mem, lambda j, c: seen.setdefault("ja", j) or None)
    assert seen["ja"] == "あい"  # exactly `length` bytes, no garbage tail


# ----------------------------------------------------------------- build_network_translate_fn routing


def _cfg(**over):
    # Defaults to the NEW "translate the rest" model (network_translate_all=True); the legacy
    # whitelist tests pass network_translate_all=False explicitly.
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=0, battle_names=False, network_translate_all=True,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


def _legacy_cfg(**over):
    """Stub cfg pinned to the legacy whitelist routing (network_translate_all=False)."""
    over.setdefault("network_translate_all", False)
    return _cfg(**over)


def test_network_translate_fn_non_japanese_returns_none(tmp_path):
    c = TranslationCache(tmp_path / "n.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("Bob", "<%sM_pc>") is None


def test_network_translate_fn_name_category_uses_name_path(tmp_path):
    # A name category resolves via the name path (community/cache hit wins over MT).
    c = TranslationCache(tmp_path / "n2.db")
    c.store("スライム", "Slime", "community")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("スライム", "<%sM_npc>") == "Slime"


def test_network_translate_fn_kaisetubun_uses_text_path(tmp_path):
    # The story-so-far category routes to the regular text path (whole-string community hit).
    c = TranslationCache(tmp_path / "n3.db")
    ja = "これまでのあらすじ。"
    c.store(ja, "The story so far.", "community")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t, lines_per_page=0, sync=False)
    out = fn(ja, "<%sM_kaisetubun>")
    assert out is not None and "story so far" in out.lower()


def test_is_bazaar_transaction_matches_only_buy_categories():
    from dqxclarity.runtime.dispatch import _is_bazaar_transaction

    assert _is_bazaar_transaction("This is <%dL_KAUKAZU> <%sL_KAITAI_ITEM>\nthat <%sL_URINUSI> has l")
    assert _is_bazaar_transaction("Bought <%dM_item_num> <%sM_item><%sM_plusnum> from the Bazaar fo")
    # ordinary categories are untouched
    assert not _is_bazaar_transaction("<%sM_kaisetubun>")
    assert not _is_bazaar_transaction("<%sM_npc>")
    assert not _is_bazaar_transaction("<%sM_item>")


def test_network_translate_fn_bazaar_transaction_passes_through(tmp_path):
    """Bazaar buy-confirmation/receipt categories must return None (no write) even on a CACHE HIT.

    The game reads that same buffer to execute the purchase, so any in-place write breaks buying
    (proven by an A/B against the live game). Pre-storing a hit makes this strong: without the guard
    the hit would be returned and written; with it, the category passes straight through.
    """
    c = TranslationCache(tmp_path / "bazaar.db")
    ja = "これは2 やくそう\nアリエスが出品しています。"
    c.store(ja, "TRANSLATED — MUST NOT BE WRITTEN", "community")  # a hit the guard must suppress
    t = Translator(c)
    buy_cat = "This is <%dL_KAUKAZU> <%sL_KAITAI_ITEM>\nthat <%sL_URINUSI> has l"
    for cfg in (_cfg(), _legacy_cfg()):  # default "translate-the-rest" AND legacy routings
        assert build_network_translate_fn(cfg, t)(ja, buy_cat) is None
    # control: the SAME ja under a non-bazaar category DOES return the cache hit — proving it's the
    # bazaar guard suppressing the write, not some unrelated filter.
    assert build_network_translate_fn(_cfg(), t)(ja, "<%sM_prose>") == "TRANSLATED — MUST NOT BE WRITTEN"


def test_network_translate_fn_kaisetubun_wraps_narrower_than_generic(tmp_path):
    # The Story So Far panel is narrower than the dialogue box, so the recap (<%sM_kaisetubun>) must
    # wrap to KAISETUBUN_WRAP — wider wrapping clips words off the panel's right edge. A generic
    # whitelisted category still uses the wider (46) box width.
    from dqxclarity.runtime.dispatch import KAISETUBUN_WRAP

    class LongProvider:
        name = "googletranslatefree"

        def available(self):
            return True

        def translate(self, texts):
            return [
                "This is a deliberately long story recap sentence that has to wrap to the narrow "
                "Story So Far panel rather than the wider dialogue box so no words clip off the edge."
                for _ in texts
            ]

    c = TranslationCache(tmp_path / "kw.db")
    t = Translator(c, sync_provider=LongProvider())
    # Legacy whitelist path: <%sM_00> is a generic whitelisted category, and the sync provider runs
    # inline (the translate_all path forces async, which would return None on a fresh miss).
    fn = build_network_translate_fn(_legacy_cfg(), t, wrap_width=46, lines_per_page=0, sync=True)
    recap = fn("ながいものがたりのあらすじ。", "<%sM_kaisetubun>")
    assert recap is not None
    assert max(len(line) for line in recap.split("\n")) <= KAISETUBUN_WRAP
    generic = fn("ながいものがたりのあらすじ。", "<%sM_00>")  # cache hit, wrapped at the wider 46
    assert generic is not None
    assert max(len(line) for line in generic.split("\n")) > KAISETUBUN_WRAP


def test_network_translate_fn_kaisetubun_marks_cutoff_when_taller_than_panel(tmp_path):
    # The panel is ~9 lines tall and can't scroll/sub-paginate, so a recap that wraps to more lines
    # is trimmed to the visible height with a trailing "..." cutoff marker (NOT <br> — the panel does
    # not paginate on it). A recap that fits is returned unchanged (no marker).
    from dqxclarity.runtime.dispatch import KAISETUBUN_BOX_LINES, KAISETUBUN_WRAP

    class VeryLongProvider:
        name = "googletranslatefree"

        def available(self):
            return True

        def translate(self, texts):
            return [(" ".join(["word"] * 120)) for _ in texts]  # >> 9 lines at width 38

    c = TranslationCache(tmp_path / "kp.db")
    t = Translator(c, sync_provider=VeryLongProvider())
    # Legacy path so the sync provider runs inline (translate_all forces async -> None on a miss).
    fn = build_network_translate_fn(_legacy_cfg(), t, wrap_width=46, lines_per_page=0, sync=True)
    out = fn("ながいあらすじ。", "<%sM_kaisetubun>")
    assert out is not None
    lines = out.split("\n")
    assert "<br>" not in out                       # no dead pagination
    assert len(lines) <= KAISETUBUN_BOX_LINES       # trimmed to the visible panel height
    assert lines[-1].endswith("...")                # cut-off marked
    assert all(len(ln) <= KAISETUBUN_WRAP for ln in lines)  # still within the panel width


def test_network_translate_fn_name_path_never_mts(tmp_path):
    # An uncached name in a name category must NOT hit MT — it romanizes or returns None, never the
    # text path's MT. Use a provider that would shout if called, and assert it isn't.
    from dqxclarity.translate.romanize import is_available

    class LoudProvider:
        name = "googletranslatefree"
        called = False

        def available(self):
            return True

        def translate(self, texts):
            LoudProvider.called = True
            return ["SHOULD-NOT-APPEAR" for _ in texts]

    c = TranslationCache(tmp_path / "n4.db")
    t = Translator(c, sync_provider=LoudProvider())
    fn = build_network_translate_fn(_cfg(), t, sync=True)
    out = fn("たろう", "<%sM_pc>")  # uncached player-style name in a NAME category
    assert LoudProvider.called is False  # name path never calls MT
    if is_available():
        assert out is not None and out.lower().startswith("tar")  # romanized


# ----------------------------------------------- build_network_translate_fn category filtering (regression)


def test_net_category_set_sizes_match_upstream():
    # Copied verbatim from upstream hooks/network_text.py; sizes lock the copy in.
    assert len(NET_TRANSLATE_CATEGORIES) == 28
    assert len(NET_IGNORE_CATEGORIES) == 59
    assert len(NET_NAME_CATEGORIES) == 17
    assert len(NET_GENERIC_CATEGORIES) == 9
    # NAME + GENERIC + kaisetubun are all whitelisted (routed, not passed through).
    assert NET_NAME_CATEGORIES <= NET_TRANSLATE_CATEGORIES
    assert NET_GENERIC_CATEGORIES <= NET_TRANSLATE_CATEGORIES
    assert "<%sM_kaisetubun>" in NET_TRANSLATE_CATEGORIES


def test_network_translate_fn_battle_category_passes_through(tmp_path):
    # CORE REGRESSION GUARD (LEGACY whitelist path): a battle/ignore category with a Japanese value
    # is NEVER translated. This is what stopped player/monster names being mangled every combat hit.
    # (Under the new translate_all model a battle name tag routes to the instant name-ify pass — see
    # test_translate_all_battle_template_name_ified — but the toggle=False path keeps the drop.)
    c = TranslationCache(tmp_path / "battle.db")
    t = Translator(c)
    fn = build_network_translate_fn(_legacy_cfg(), t)
    assert "<%sB_ACTOR>" in NET_IGNORE_CATEGORIES
    assert fn("スライム", "<%sB_ACTOR>") is None  # battle actor name left untouched
    assert fn("ホイミ", "<%sB_ACTION>") is None  # battle action left untouched


def test_network_translate_fn_unknown_category_passes_through(tmp_path):
    # LEGACY whitelist path: an unknown category (in NO upstream set) is non-whitelisted -> pass
    # through. (Under translate_all the SAME unknown JP category is instead sent to the async text
    # path — see test_translate_all_unknown_prose_goes_to_text_path.)
    c = TranslationCache(tmp_path / "unknown.db")
    t = Translator(c)
    fn = build_network_translate_fn(_legacy_cfg(), t)
    cat = "<%sTOTALLY_UNKNOWN>"
    assert cat not in NET_TRANSLATE_CATEGORIES and cat not in NET_IGNORE_CATEGORIES
    assert fn("なにかの にほんご。", cat) is None


def test_network_translate_fn_version_noise_passes_through(tmp_path):
    # Login-screen version noise (category startswith "Version <%s_MVER") is left untouched.
    c = TranslationCache(tmp_path / "ver.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("バージョン", "Version <%s_MVER1>") is None


def test_network_translate_fn_jibun_suffix_becomes_self(tmp_path):
    # "<name> uses X on 自分" -> the 自分 suffix is replaced with "self".
    c = TranslationCache(tmp_path / "self.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    # Use a whitelisted category so we'd reach later steps if the suffix rule didn't fire first;
    # the 自分 rule runs before the whitelist check (mirrors upstream order).
    assert fn("ホイミを 自分", "<%sM_00>") == "ホイミを self"
    # Even a NAME-category value ending in 自分 gets the suffix swap (upstream order).
    assert fn("自分", "<%sM_pc>") == "self"


def test_network_translate_fn_generic_string_translates(tmp_path):
    # A whitelisted GENERIC-STRING category with a community hit returns the curated EN (text path).
    c = TranslationCache(tmp_path / "gen.db")
    ja = "やくそう"
    c.store(ja, "Medicinal Herb", "community")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t, lines_per_page=0, sync=False)
    assert "<%sM_item>" in NET_GENERIC_CATEGORIES
    assert fn(ja, "<%sM_item>") == "Medicinal Herb"


def test_network_translate_fn_kaisetubun_mt_fallback(tmp_path):
    # INTERIM MT FALLBACK: <%sM_kaisetubun> with Japanese and NO community entry DOES translate
    # via MT (deviates from upstream static-only; documented in dispatch.py). Deterministic stub.
    class FastProvider:
        name = "googletranslatefree"

        def available(self):
            return True

        def translate(self, texts):
            return ["Once upon a time." for _ in texts]

    c = TranslationCache(tmp_path / "kai.db")
    t = Translator(c, sync_provider=FastProvider())
    # Legacy path so the SYNC provider translates inline (translate_all forces async -> None on miss;
    # the async behaviour is covered by test_translate_all_unknown_prose_goes_to_text_path).
    fn = build_network_translate_fn(_legacy_cfg(), t, lines_per_page=0, sync=True)
    out = fn("むかしむかし、あるところに。", "<%sM_kaisetubun>")
    assert out is not None and "once upon a time" in out.lower()


# ------------------------------------------------ FEATURE #12: "translate the rest" (network_translate_all)


def test_is_name_category_true_for_name_bearing_categories():
    # Battle + simple-name + map-name categories are all detected as name-bearing.
    for cat in (
        "<%sB_ACTOR>", "<%sB_TARGET>", "<%sB_TARGET2>",
        "<%sM_pc>", "<%sM_npc>", "<%sC_PC>",
        "<%sM_name>", "<%sM_NAME>", "<%sL_SENDER_NAME>", "<%sL_HIRYU_NAME>",
        "<%sM_OWNER>", "<%sL_OWNER>", "<%sL_URINUSI>", "<%sM_hiryu>", "<%sL_HIRYU>",
        "<%sL_MONSTERNAME>", "<%sC_MERCENARY>",
        "<%sCAS_gambler>", "<%sCAS_target>", "<%sW_MAP_NAME>", "<%sM_monster>",
    ):
        assert _is_name_category(cat), cat
    # Every curated NAME category is covered.
    assert all(_is_name_category(c) for c in NET_NAME_CATEGORIES)
    # A battle TEMPLATE that merely embeds a tag is caught by the substring match.
    assert _is_name_category("\\sしびれくらげ\\e <%sB_TARGET> takes <%dB_VALUE> damage!")


def test_is_name_category_false_for_noise_categories():
    # Numeric/date/value/version/tag noise categories are NOT name-bearing -> handled by is_japanese
    # (numbers) or NET_IGNORE, never the name path.
    for cat in (
        "<%sB_VALUE>", "<%sB_VALUE2>", "<%sB_RANK>", "<%sParam1>", "<%sParam2>",
        "<%sM_num1>", "<%sM_plusnum>", "<%sM_dot>", "<%sM_caption>", "<%sM_chat>",
        "<%s_MVER1>", "<%sW_DELIMITER>", "<%sB_ACTION>", "<%sM_00>", "<%sM_kaisetubun>",
    ):
        assert not _is_name_category(cat), cat
    # Sanity: a handful of the real NET_IGNORE numeric/value categories aren't mis-detected as names.
    for cat in NET_IGNORE_CATEGORIES:
        if "VALUE" in cat or "Param" in cat or cat in ("<%sM_dot>", "<%sM_num1>", "<%sM_plusnum>"):
            assert not _is_name_category(cat), cat


def test_translate_all_noise_non_japanese_returns_none(tmp_path):
    # (a) A NOISE category whose captured value is NOT Japanese (numbers / <@M_..> tag / English) is
    # dropped by is_japanese -> None, exactly as before (the whitelist was redundant for this).
    c = TranslationCache(tmp_path / "ta_noise.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("12345", "<%sB_VALUE>") is None
    assert fn("<@M_スライム>", "<%sM_emote>") is None  # tag noise (no Japanese run outside the tag)
    assert fn("Bob", "<%sM_pc>") is None              # already-English name


def test_translate_all_name_category_player_sub_wins_over_monster_collision(tmp_path):
    # (b) The player タイカン ("Taikan") collides with a cached MONSTER タイカン ("Squid"). In a NAME
    # category the player-substitution must win -> "Taikan", NOT the colliding "Squid". This is the
    # headline correctness case; the name path is INSTANT (no MT).
    c = TranslationCache(tmp_path / "ta_collide.db")
    c.store("タイカン", "Squid", "community")  # the colliding monster name in the cache
    t = Translator(c)
    t.player_name_ja, t.player_name_en = "タイカン", "Taikan"
    # translate_name would return the cached "Squid"; player-sub in _translate_name_runs precedes it.
    assert t.translate_name("タイカン") == "Squid"
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("タイカン", "<%sM_pc>") == "Taikan"


def test_translate_all_battle_template_is_name_ified(tmp_path):
    # (c) A battle template (category embeds <%sB_TARGET>) name-ifies its Japanese run via the instant
    # name path — community hit here — instead of being dropped (legacy) or MT'd (would mangle/lag).
    c = TranslationCache(tmp_path / "ta_battle.db")
    c.store("しびれくらげ", "Stingue", "community")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    out = fn("しびれくらげ", "<%sB_TARGET>")
    assert out == "Stingue"  # name-ified (NOT None, NOT MT)


def test_translate_all_unknown_prose_goes_to_async_text_path(tmp_path):
    # (d) An UNKNOWN Japanese prose category is NO LONGER dropped — it routes to the ASYNC text path:
    # a cache MISS enqueues a background request and returns None (filled on a later view), WITHOUT
    # blocking. We assert it enqueues (so it's translate-the-rest, not the old silent drop) and that a
    # SYNC provider is NOT called inline (proving the prose path is async even though the hook is sync).
    class LoudSyncProvider:
        name = "googletranslatefree"
        called = False

        def available(self):
            return True

        def translate(self, texts):
            LoudSyncProvider.called = True
            return ["INLINE-MT" for _ in texts]

    c = TranslationCache(tmp_path / "ta_prose.db")
    t = Translator(c, sync_provider=LoudSyncProvider())
    # sync=True mirrors the network_text HookSpec; translate_all must still force the prose path async.
    fn = build_network_translate_fn(_cfg(), t, lines_per_page=0, sync=True)
    cat = "<%sお知らせ>"  # made-up "Important Notice"-style prose category (not name/ignore/version)
    assert cat not in NET_NAME_CATEGORIES and cat not in NET_IGNORE_CATEGORIES
    out = fn("だいじなおしらせがあります。", cat)
    assert out is None                       # cache miss -> enqueued + None (NOT dropped, NOT inline)
    assert LoudSyncProvider.called is False  # prose path is async -> no inline MT (no game-thread lag)
    # The miss was ENQUEUED for background MT (translate-the-rest), proving it isn't a silent drop.
    assert t._q.qsize() > 0

    # And a cache HIT on the same async path returns the community EN immediately.
    c.store("だいじなおしらせがあります。", "There is an important notice.", "community")
    assert fn("だいじなおしらせがあります。", cat) == "There is an important notice."


def test_translate_all_kaisetubun_prose_is_not_dropped(tmp_path):
    # (d') The Story So Far recap (<%sM_kaisetubun>) also flows to the async text path: a community hit
    # renders immediately; it is NOT dropped.
    c = TranslationCache(tmp_path / "ta_kai.db")
    ja = "これまでのあらすじ。"
    c.store(ja, "The story so far.", "community")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t, lines_per_page=0)
    out = fn(ja, "<%sM_kaisetubun>")
    assert out is not None and "story so far" in out.lower()


def test_translate_all_net_ignore_category_still_dropped(tmp_path):
    # (e) A NET_IGNORE category (high-volume JP chat noise) with Japanese is STILL dropped -> None.
    c = TranslationCache(tmp_path / "ta_ignore.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert "<%sM_chat>" in NET_IGNORE_CATEGORIES
    assert fn("こんにちは みなさん。", "<%sM_chat>") is None


def test_translate_all_version_noise_returns_none(tmp_path):
    # (f) Login-screen version noise is left untouched in the translate_all path too.
    c = TranslationCache(tmp_path / "ta_ver.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("バージョン", "Version <%s_MVER1>") is None


def test_translate_all_jibun_suffix_becomes_self(tmp_path):
    # The 自分 -> "self" nicety still fires for a non-name, non-ignored category. (It now runs AFTER
    # the name + NET_IGNORE checks so an ignored chat line ending in 自分 is dropped first.)
    c = TranslationCache(tmp_path / "ta_self.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("ホイミを 自分", "<%sM_00>") == "ホイミを self"


def test_translate_all_quest_name_category_is_text_not_romaji(tmp_path):
    # REGRESSION GUARD: <%sEV_QUEST_NAME> is a quest TITLE (prose, NET_GENERIC), NOT a name. The old
    # broad "_NAME>" substring wrongly name-ified it -> romaji. It must take the async TEXT path: a
    # cache miss returns None (a name-ify miss would instead return a romanized, non-None string).
    assert _is_name_category("<%sEV_QUEST_NAME>") is False
    c = TranslationCache(tmp_path / "ta_qn.db")
    t = Translator(c)  # no provider: text-path miss -> None; name-path miss -> romaji (non-None)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("ほのおの洞くつ", "<%sEV_QUEST_NAME>") is None


def test_translate_all_map_name_is_name_ified(tmp_path):
    # <%sW_MAP_NAME> was DROPPED by the legacy whitelist (it's in NET_IGNORE); under translate_all a
    # zone name is a proper noun -> name-ify (community/romaji), reached BEFORE the NET_IGNORE drop.
    c = TranslationCache(tmp_path / "ta_map.db")
    c.store("ジュレット", "Julet", "community")
    t = Translator(c)
    assert _is_name_category("<%sW_MAP_NAME>") is True
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("ジュレット", "<%sW_MAP_NAME>") == "Julet"  # name-ified, NOT dropped


def test_translate_all_chat_ending_in_jibun_is_still_dropped(tmp_path):
    # ORDERING FIX: NET_IGNORE drops BEFORE the 自分 transform, so a chat line that happens to end in
    # 自分 is dropped (None), NOT mangled into "...self".
    c = TranslationCache(tmp_path / "ta_chatself.db")
    t = Translator(c)
    assert "<%sM_chat>" in NET_IGNORE_CATEGORIES
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("みんな がんばろう 自分", "<%sM_chat>") is None


def test_translate_all_battle_self_target_jibun_becomes_self(tmp_path):
    # A battle self-target arg "自分" in a NAME category resolves to "self" INSIDE the name-ify pass
    # (not romanized), so the 自分 nicety survives the reorder for name categories too.
    c = TranslationCache(tmp_path / "ta_selftgt.db")
    t = Translator(c)
    fn = build_network_translate_fn(_cfg(), t)
    assert fn("自分", "<%sB_TARGET>") == "self"


def test_translate_all_hybrid_whitelist_sync_rest_async(tmp_path):
    # THE HYBRID FIX (for the live "more Japanese" backslide): a WHITELISTED prose category stays
    # SYNC — a cache miss is translated INLINE by the sync provider (immediate, no JA-flash), exactly
    # like the legacy path. A NON-whitelisted category goes ASYNC — the sync provider is NOT called
    # inline (enqueued, fills later). So translate-the-rest never makes the known-good whitelist async.
    class RecordProvider:
        name = "googletranslatefree"

        def __init__(self):
            self.calls = []

        def available(self):
            return True

        def translate(self, texts):
            self.calls += list(texts)
            return ["EN-" + str(i) for i, _ in enumerate(texts)]

    c = TranslationCache(tmp_path / "ta_hybrid.db")
    p = RecordProvider()
    t = Translator(c, sync_provider=p)
    fn = build_network_translate_fn(_cfg(), t, sync=True)  # network_text HookSpec sync=True
    # whitelisted generic (<%sM_header> is in NET_TRANSLATE, not a name) -> SYNC: inline MT on miss.
    assert "<%sM_header>" in NET_TRANSLATE_CATEGORIES and not _is_name_category("<%sM_header>")
    out_wl = fn("だいじなみだし。", "<%sM_header>")
    assert out_wl is not None and p.calls  # translated INLINE by the sync provider (immediate)
    # non-whitelisted prose -> ASYNC: miss returns None and does NOT call the sync provider inline.
    p.calls.clear()
    nonwl = "<%sお知らせ>"
    assert nonwl not in NET_TRANSLATE_CATEGORIES and not _is_name_category(nonwl)
    assert fn("べつのおしらせ。", nonwl) is None  # async miss -> None
    assert p.calls == []                          # NOT inline (enqueued instead) -> no game-thread lag
