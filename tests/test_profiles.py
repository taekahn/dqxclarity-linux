"""Per-surface format profiles (P1): different text surfaces wrap/paginate/translate differently.

The dialogue box paginates on ``<br>`` and blocks for a fast synchronous MT; the quest menu renders
``<br>`` literally and reads several fields per open, so it must NOT insert ``<br>`` and must not
block the menu on a slow translation. These tests lock that in without touching the game or network.
"""

from __future__ import annotations

import struct
import threading

from types import SimpleNamespace

from dqxclarity.process.detour import STATE_DONE, STATE_REQUEST, BlockingHook
from dqxclarity.process.hooks import HOOKS
from dqxclarity.runtime.dispatch import build_translate_fn, serve
from dqxclarity.translate.db import TranslationCache
from dqxclarity.translate.dialogue import translate_conversation
from dqxclarity.translate.pipeline import Translator
from dqxclarity.translate.wrap import add_page_breaks


def _cfg(**over):
    """Minimal config stub: build_translate_fn only reads cfg.translate.* attributes."""
    tr = SimpleNamespace(
        player_name_ja="", player_name_en="", sibling_name_ja="", sibling_name_en="",
        wrap_width=46, lines_per_page=3,
    )
    for k, v in over.items():
        setattr(tr, k, v)
    return SimpleNamespace(translate=tr)


# --------------------------------------------------------------------------- wrap.add_page_breaks


def test_add_page_breaks_zero_lines_per_page_inserts_no_br():
    text = "a\nb\nc\nd\ne\nf"
    # lines_per_page=0 disables pagination entirely (and must not divide-by-zero).
    assert add_page_breaks(text, lines_per_page=0) == text
    assert "<br>" not in add_page_breaks(text, lines_per_page=0)


def test_add_page_breaks_negative_lines_per_page_inserts_no_br():
    text = "a\nb\nc\nd"
    assert add_page_breaks(text, lines_per_page=-1) == text


def test_add_page_breaks_positive_still_paginates():
    # Regression guard: the default/dialogue behaviour is unchanged for lines_per_page >= 1.
    assert add_page_breaks("a\nb\nc\nd\ne", lines_per_page=3).split("\n") == [
        "a", "b", "c", "<br>", "d", "e",
    ]


# ----------------------------------------------------------------- dialogue.translate_conversation


def test_translate_conversation_zero_lpp_no_br_community_cached(tmp_path):
    """Community-cached per-segment hit: long EN never gets a <br> when lines_per_page=0."""
    c = TranslationCache(tmp_path / "q.db")
    long_en = (
        "Defeat the slimes roaming the plains and report back to the guild master in town "
        "for your reward and the next step of this rather wordy quest objective."
    )
    # store the cached segment under its normalized JA key
    from dqxclarity.translate.wrap import normalize_source

    c.store(normalize_source("スライムを たおして ギルドに もどってください。"), long_en, "community")
    t = Translator(c)
    out = translate_conversation(
        t, "スライムを たおして ギルドに もどってください。", width=46, lines_per_page=0
    )
    assert out is not None
    assert "<br>" not in out  # quest menu renders <br> literally -> must be absent


def test_translate_conversation_zero_lpp_no_br_mt_path(tmp_path):
    """MT (sync) path: a long machine translation is wrapped but gets no <br> at lines_per_page=0."""

    class FastProvider:
        name = "googletranslatefree"

        def available(self):
            return True

        def translate(self, texts):
            # return a long English string so wrapping would normally trigger pagination
            return [
                "This is a deliberately very long machine translated quest description that "
                "would wrap across several lines and would normally get page breaks inserted."
                for _ in texts
            ]

    c = TranslationCache(tmp_path / "mt.db")
    t = Translator(c, sync_provider=FastProvider())
    out = translate_conversation(t, "ながい クエストの せつめい。", width=46, lines_per_page=0, sync=True)
    assert out is not None
    assert "<br>" not in out  # no pagination markers
    # still wrapped to width (no single line exceeds the box width)
    assert all(len(line) <= 46 for line in out.split("\n"))


def test_translate_conversation_dialogue_default_still_paginates(tmp_path):
    """Regression: the dialogue profile (lines_per_page=3) still inserts <br>."""

    class FastProvider:
        name = "googletranslatefree"

        def available(self):
            return True

        def translate(self, texts):
            return [
                "A long enough machine translation that spans well over three wrapped lines so "
                "the dialogue paginator inserts at least one page break marker into the output."
                for _ in texts
            ]

    c = TranslationCache(tmp_path / "d.db")
    t = Translator(c, sync_provider=FastProvider())
    out = translate_conversation(t, "ながい かいわ。", width=46, lines_per_page=3, sync=True)
    assert out is not None and "<br>" in out


# ------------------------------------------------------------------------------- the quest HookSpec


def test_quest_hookspec_profile():
    q = HOOKS["quest"]
    assert q.lines_per_page == 0  # quest menu renders <br> literally -> no pagination
    assert q.sync is False        # async so a slow line never freezes the menu
    assert q.wrap_width == 46


def test_dialogue_hookspec_profile_unchanged():
    d = HOOKS["dialogue"]
    assert d.wrap_width == 46 and d.lines_per_page == 3 and d.sync is True


# -------------------------------------------------- build_translate_fn community whole-string path


def test_translate_fn_strips_br_from_community_hit_when_no_pagination(tmp_path):
    """A whole-string community hit carrying <br> must NOT render literally on the quest surface.

    Community quest/event strings carry <br>; the quest profile (lines_per_page=0) renders it
    literally, so translate_fn strips it just like the MT path does.
    """
    c = TranslationCache(tmp_path / "cw.db")
    ja = "コルット地方で新種発見？"
    c.store(ja, "Line one<br>Line two", "community")
    t = Translator(c)
    # quest profile: no pagination
    fn_q, _ = build_translate_fn(_cfg(), t, lines_per_page=0, sync=False)
    out_q = fn_q(ja)
    assert out_q is not None and "<br>" not in out_q
    # dialogue profile keeps <br> (the dialogue box paginates on it)
    fn_d, _ = build_translate_fn(_cfg(), t, lines_per_page=3, sync=True)
    assert "<br>" in fn_d(ja)


# --------------------------------------------------------------------- serve() with per-hook fns


class _ServeMem:
    """Address-space stub driving real BlockingHooks: per-hook STATE/SLOT u32s + text buffers."""

    def __init__(self) -> None:
        self.u32: dict[int, int] = {}
        self.buffers: dict[int, bytes] = {}

    def read_u32(self, addr: int) -> int:
        return self.u32.get(addr, 0)

    def read(self, addr: int, size: int) -> bytes:
        return self.buffers.get(addr, b"")[:size]

    def write(self, addr: int, data: bytes) -> None:
        if addr in self.u32 or len(data) == 4:
            # STATE writes are 4-byte dwords; record them so serve_once's release is visible.
            self.u32[addr] = struct.unpack("<I", data[:4])[0]
        self.buffers[addr] = bytes(data)


def test_serve_drives_two_hooks_each_with_own_translate_fn():
    """serve() polls (name, hook, translate_fn) triples; each hook uses its OWN translate_fn."""
    mem = _ServeMem()
    # hook A (dialogue-like): state/slot/buffer
    a_state, a_slot, a_ptr = 0x10, 0x14, 0x1000
    b_state, b_slot, b_ptr = 0x20, 0x24, 0x2000
    mem.u32[a_state] = STATE_REQUEST
    mem.u32[a_slot] = a_ptr
    mem.buffers[a_ptr] = "あ".encode() + b"\x00" + b"\x00" * 60
    mem.u32[b_state] = STATE_REQUEST
    mem.u32[b_slot] = b_ptr
    mem.buffers[b_ptr] = "い".encode() + b"\x00" + b"\x00" * 60

    hook_a = BlockingHook(0x400000, 0, a_state, a_slot, 0, b"", fields=((0, 60),))
    hook_b = BlockingHook(0x500000, 0, b_state, b_slot, 0, b"", fields=((0, 60),))

    seen: list[tuple[str, str]] = []

    def fn_a(ja):  # dialogue surface translate_fn
        return "DIALOGUE-EN"

    def fn_b(ja):  # quest surface translate_fn (distinct from A)
        return "QUEST-EN"

    triples = [("dialogue", hook_a, fn_a), ("quest", hook_b, fn_b)]

    stop = threading.Event()
    # Stop as soon as both requests have been served (both states released to DONE).
    def watcher():
        import time

        deadline = time.time() + 2
        while time.time() < deadline:
            if mem.u32.get(a_state) == STATE_DONE and mem.u32.get(b_state) == STATE_DONE:
                break
            time.sleep(0.001)
        stop.set()

    t = threading.Thread(target=watcher)
    t.start()
    served = serve(mem, triples, stop=stop, on_line=lambda name, ja: seen.append((name, ja)))
    t.join()

    assert served >= 2
    # each hook wrote back using its OWN translate_fn
    assert mem.buffers[a_ptr].startswith(b"DIALOGUE-EN\x00")
    assert mem.buffers[b_ptr].startswith(b"QUEST-EN\x00")
    # both released the game thread (I2/I4)
    assert mem.u32[a_state] == STATE_DONE and mem.u32[b_state] == STATE_DONE
    names_served = {name for name, _ in seen}
    assert names_served == {"dialogue", "quest"}
