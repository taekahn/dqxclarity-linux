"""Tests for the startup "Important Notice" scanner (backlog #27) — offline, no game, no network.

The Important Notice body does NOT flow through any code hook (it's absent from a full
``network_text`` capture); it is a STATIC, null-terminated UTF-8 buffer in game memory. So it is
handled by a memory SCANNER (find → read → translate → write back), mirroring the name scanner.

These tests exercise that scanner against a FAKE mem backed by an in-memory byte buffer seeded with
the REAL probed sample shape:

  * a formatting prefix of control bytes ``@D\\xd8\\x02`` (40 44 D8 02) that ISN'T valid standalone
    UTF-8 and must survive byte-for-byte;
  * two pages joined by the literal ``\\n<PAGE>\\n`` page-break token;
  * the ``動画配信の際はサーバー`` anchor (``NOTICE_STRING``) mid-buffer;
  * ``https://sqex.to/iDo`` URLs that must survive untranslated.

Plus the per-attach run() wiring (the notice scanner starts/stops once per attach), reusing the
hermetic ``run_env`` harness.
"""

from __future__ import annotations

import threading

from dqxclarity import cli
from dqxclarity.process.signatures import NOTICE_STRING
from dqxclarity.runtime import notice_loop

# Reuse the fully-stubbed run() harness + serve outcome helpers from the lifecycle suite.
from test_lifecycle import _user_quit, run_env  # noqa: F401  (run_env is a fixture)


# --------------------------------------------------------------------------------------------- #
# The real sample body shape, byte-accurate.                                                    #
# --------------------------------------------------------------------------------------------- #

# Verbatim formatting prefix probed from the live buffer (40 44 D8 02). The D8 byte is NOT valid
# standalone UTF-8, so the scanner must handle the prefix as raw bytes — that's the headline trap.
PREFIX = b"\x40\x44\xd8\x02"

# Page 1 embeds the anchor phrase (動画配信の際はサーバー) and a URL; page 2 is a second screen.
PAGE1_JA = "「お知らせ」動画配信の際はサーバーの情報を https://sqex.to/iDo でご確認ください。"
PAGE2_JA = "冒険者のみなさまへ。よろしくお願いいたします。 https://sqex.to/iDo"

# The two pages joined by the literal page-break token, exactly as the game stores it.
BODY_JA = PAGE1_JA + "\n<PAGE>\n" + PAGE2_JA

# Curated EN per whole page (the prose pipeline translates each PAGE as a unit). The URL is shielded
# before MT, so the fake fn never sees the URL — it's restored verbatim afterward. Note: splitting on
# the literal ``<PAGE>`` keeps the surrounding ``\n`` attached to each page (the game stores
# ``…\n<PAGE>\n…``), so the keys here carry that trailing/leading newline — exactly what a real MT fn
# would receive. We strip+rematch on the trimmed body so the test stays readable.
_PAGE_EN = {
    "「お知らせ」動画配信の際はサーバーの情報を \x01URL0\x01 でご確認ください。":
        "[Notice] For streaming, please check the server info at \x01URL0\x01.",
    "冒険者のみなさまへ。よろしくお願いいたします。 \x01URL0\x01":
        "To all adventurers: thank you for your support. \x01URL0\x01",
}


def fake_translate_fn(shielded: str) -> str | None:
    """Stand-in prose fn: maps a known (URL-shielded) JA page to EN, else None (leave unchanged).

    The page split keeps the ``\\n`` around ``<PAGE>`` attached to each page, so we match on the
    STRIPPED page and re-attach the same surrounding whitespace to the EN — mirroring how a real
    wrapping MT fn preserves leading/trailing structure.
    """
    core = shielded.strip("\n")
    en = _PAGE_EN.get(core)
    if en is None:
        return None
    # Re-attach the exact surrounding newlines so the <PAGE> delimiter round-trips byte-for-byte.
    lead = shielded[: len(shielded) - len(shielded.lstrip("\n"))]
    trail = shielded[len(shielded.rstrip("\n")):]
    return lead + en + trail


class FakeMem:
    """In-memory byte buffer exposing the LinuxProcessMemory surface the notice scanner uses.

    The buffer is laid out as: some leading NUL-terminated junk, then the notice buffer (prefix +
    body + NUL), so ``find_notice``'s backward-NUL scan from the anchor lands on the real start.
    """

    def __init__(self, body_bytes: bytes, *, lead: bytes | None = None) -> None:
        # base offset keeps addresses non-zero/realistic. The lead is >2 KiB of mapped data ending in
        # a NUL right before the notice — mirroring the live buffer (the notice sits deep in a mapped
        # region) so find_notice's 2 KiB backward window stays within mapped memory and lands on the
        # NUL just before the notice start.
        if lead is None:
            lead = b"A" * 3000 + b"\x00"
        self.base = 0x100000
        self.notice_start = self.base + len(lead)
        self.buf = lead + body_bytes + b"\x00"
        self.writes: list[tuple[int, bytes]] = []

    def _abs(self, addr: int) -> int:
        return addr - self.base

    def pattern_scan(self, pattern, *, data_only=False, return_multiple=True, limit=None):
        # Match the anchor as a raw byte substring; return its ABSOLUTE address.
        import re

        rx = re.compile(pattern, re.DOTALL)
        m = rx.search(self.buf)
        if not m:
            return None if not return_multiple else []
        addr = self.base + m.start()
        return addr if not return_multiple else [addr]

    def read(self, addr: int, size: int) -> bytes:
        off = self._abs(addr)
        if off < 0:
            return b""
        return self.buf[off:off + size]

    def read_cstring(self, addr: int, max_len: int = 512, encoding: str = "utf-8") -> str:
        raw = self.read(addr, max_len)
        end = raw.find(b"\x00")
        if end != -1:
            raw = raw[:end]
        return raw.decode(encoding, "replace")

    def write(self, addr: int, data: bytes) -> int:
        off = self._abs(addr)
        self.buf = self.buf[:off] + data + self.buf[off + len(data):]
        self.writes.append((addr, data))
        return len(data)

    # The scanner's translate_notice does its own raw read/write; expose write_cstring too for parity
    # with the real backend (not used by the notice path, but harmless to have).
    def write_cstring(self, addr: int, text: str, *, max_bytes: int, encoding: str = "utf-8") -> bool:
        data = text.encode(encoding, "replace") + b"\x00"
        if len(data) > max_bytes:
            return False
        data += b"\x00" * (max_bytes - len(data))
        self.write(addr, data)
        return True


def _written_notice(mem: FakeMem) -> bytes:
    """Read back the (post-write) notice buffer up to its NUL."""
    raw = mem.read(mem.notice_start, 4096)
    return raw[: raw.find(b"\x00")]


# =============================================================================================== #
# find_notice: start computed from the anchor                                                     #
# =============================================================================================== #


def test_find_notice_computes_start_from_anchor():
    mem = FakeMem(PREFIX + BODY_JA.encode("utf-8"))
    start = notice_loop.find_notice(mem)
    assert start == mem.notice_start  # start is the byte AFTER the previous NUL, before the prefix

    # The anchor really is mid-buffer (not at the start) — proving the backward scan was needed.
    anchor = mem.pattern_scan(NOTICE_STRING, return_multiple=False)
    assert anchor > start


def test_find_notice_returns_none_when_anchor_absent():
    mem = FakeMem("ただのテキストです".encode("utf-8"))  # no NOTICE_STRING anchor
    assert notice_loop.find_notice(mem) is None


# =============================================================================================== #
# translate_notice: the full preserve pipeline + idempotency + budget                             #
# =============================================================================================== #


def test_translate_notice_preserves_prefix_pages_urls_and_replaces_ja():
    mem = FakeMem(PREFIX + BODY_JA.encode("utf-8"))
    assert notice_loop.translate_notice(mem, fake_translate_fn) is True

    out = _written_notice(mem)
    # The non-UTF-8 control prefix survived byte-for-byte.
    assert out.startswith(PREFIX)
    # The literal <PAGE> page-break token survived (translated as TWO independent pages).
    assert b"\n<PAGE>\n" in out
    # Both URLs survived untranslated (shielded around MT, restored after).
    assert out.count(b"https://sqex.to/iDo") == 2
    # The JA is gone; the EN is in.
    decoded = out[len(PREFIX):].decode("utf-8")
    assert "[Notice]" in decoded and "all adventurers" in decoded
    assert "動画配信" not in decoded  # the Japanese anchor phrase was replaced
    # The whole written buffer fits the original byte budget (+NUL) — never overflowed.
    assert len(out) + 1 <= len(PREFIX) + len(BODY_JA.encode("utf-8")) + 1


def test_translate_notice_is_idempotent_on_already_english_buffer():
    # A buffer that is already English (no Japanese) is left UNTOUCHED — no re-translate/re-write.
    en_body = "[Notice] already translated. https://sqex.to/iDo"
    mem = FakeMem(PREFIX + en_body.encode("utf-8"))
    # Seed pattern_scan to still "find" something only if the anchor is present; it isn't here, so
    # find_notice returns None and we short-circuit. Assert no write either way.
    assert notice_loop.translate_notice(mem, fake_translate_fn) is False
    assert mem.writes == []


def test_translate_notice_idempotent_when_anchor_present_but_body_not_japanese():
    # Edge: the anchor bytes are present but the readable body is already English. The language gate
    # must still skip (no re-write). We build a buffer whose decoded form has the anchor bytes but no
    # CJK in the human-readable sense is impossible (the anchor IS CJK), so instead assert the normal
    # idempotency path: translating twice writes only ONCE, and the second pass is a no-op.
    mem = FakeMem(PREFIX + BODY_JA.encode("utf-8"))
    assert notice_loop.translate_notice(mem, fake_translate_fn) is True
    first_writes = len(mem.writes)
    # Second pass: the buffer is now English -> gate skips, nothing more is written.
    assert notice_loop.translate_notice(mem, fake_translate_fn) is False
    assert len(mem.writes) == first_writes  # exactly one write total across both passes


def test_translate_notice_skips_over_budget_en_not_truncate():
    # An EN longer than the JA byte budget must be SKIPPED (write nothing), never truncated/overflow.
    mem = FakeMem(PREFIX + BODY_JA.encode("utf-8"))
    budget = len(PREFIX) + len(BODY_JA.encode("utf-8"))

    def huge_translate_fn(shielded: str) -> str:
        # Return EN far larger than any page's JA byte span -> total payload blows the budget.
        return "X" * (budget + 100)

    assert notice_loop.translate_notice(mem, huge_translate_fn) is False
    assert mem.writes == []  # nothing written
    # The original JA buffer is intact (no partial/truncated write corrupted it).
    assert _written_notice(mem) == PREFIX + BODY_JA.encode("utf-8")


def test_translate_notice_no_op_when_translation_unchanged():
    # A translate fn that returns None for every page -> no change -> no write.
    mem = FakeMem(PREFIX + BODY_JA.encode("utf-8"))
    assert notice_loop.translate_notice(mem, lambda s: None) is False
    assert mem.writes == []


def test_translate_notice_reread_guard_skips_when_buffer_changes(monkeypatch):
    # If the buffer changes between the initial read and the pre-write re-read, skip (don't clobber).
    mem = FakeMem(PREFIX + BODY_JA.encode("utf-8"))

    calls = {"n": 0}
    real_read = notice_loop._read_raw_cstring

    def flaky_read(m, addr, max_len):
        calls["n"] += 1
        if calls["n"] == 2:  # the re-read guard read -> pretend the buffer changed
            return b"DIFFERENT"
        return real_read(m, addr, max_len)

    monkeypatch.setattr(notice_loop, "_read_raw_cstring", flaky_read)
    assert notice_loop.translate_notice(mem, fake_translate_fn) is False
    assert mem.writes == []


# =============================================================================================== #
# translate_body: page-split + URL-shield + prefix in isolation                                   #
# =============================================================================================== #


def test_translate_body_splits_pages_independently():
    raw = PREFIX + BODY_JA.encode("utf-8")
    out = notice_loop.translate_body(raw, fake_translate_fn)
    assert out is not None
    # Exactly one <PAGE> delimiter survives between the two translated pages.
    assert out.count(b"<PAGE>") == 1


def test_translate_body_keeps_untranslated_page_verbatim():
    # If page 2 has no curated EN, it stays Japanese while page 1 translates — partial round-trip.
    raw = PREFIX + BODY_JA.encode("utf-8")

    def page1_only(shielded: str) -> str | None:
        # Translate only page 1 (the お知らせ page); return None for page 2 so it stays verbatim.
        return fake_translate_fn(shielded) if "お知らせ" in shielded else None

    out = notice_loop.translate_body(raw, page1_only)
    assert out is not None
    assert b"[Notice]" in out                 # page 1 translated
    assert PAGE2_JA.encode("utf-8") in out     # page 2 left verbatim (URL + JA intact)


def test_split_prefix_bytes_isolates_non_utf8_prefix():
    raw = PREFIX + "「テスト」".encode("utf-8")
    prefix, text = notice_loop._split_prefix_bytes(raw)
    assert prefix == PREFIX            # the raw non-UTF-8 prefix is isolated byte-for-byte
    assert text == "「テスト」"          # the readable text decodes cleanly, starting at 「


# =============================================================================================== #
# start_notice_scanner: the per-attach helper                                                     #
# =============================================================================================== #


def test_start_notice_scanner_enabled_starts_thread():
    seen = {}
    started = threading.Event()

    def fake_run(mem, translate_fn, *, stop, interval, on_write):
        seen.update(mem=mem, translate_fn=translate_fn, interval=interval,
                    on_write=on_write, stop=stop)
        started.set()
        stop.wait()

    import dqxclarity.runtime.notice_loop as nl
    orig = nl.run
    nl.run = fake_run
    try:
        mem = object()
        tfn = lambda ja: None  # noqa: E731
        cb = lambda: None  # noqa: E731
        handle = nl.start_notice_scanner(mem, tfn, enabled=True, interval=0.25, on_write=cb)
        assert started.wait(2.0)
        assert handle.thread is not None and handle.thread.is_alive()
        assert handle.thread.daemon is True
        assert seen["mem"] is mem
        assert seen["translate_fn"] is tfn
        assert seen["interval"] == 0.25
        assert seen["on_write"] is cb
        assert seen["stop"] is handle.stop and not handle.stop.is_set()
        handle.stop_and_join(timeout=2.0)
        assert handle.stop.is_set() and not handle.thread.is_alive()
    finally:
        nl.run = orig


def test_start_notice_scanner_disabled_starts_no_thread():
    handle = notice_loop.start_notice_scanner(object(), lambda ja: None, enabled=False)
    assert handle.thread is None
    handle.stop_and_join(timeout=1.0)  # safe no-op
    assert handle.stop.is_set()


# =============================================================================================== #
# run() wiring: the --notice flag drives the per-attach scanner                                    #
# =============================================================================================== #


def test_run_notice_defaults_false_and_scanner_disabled(run_env):  # noqa: F811
    # #27: the notice scanner is OFF by default (its anchor is wrong + poking the volatile notice
    # buffer can crash the game). The handle is still constructed+stop_and_join'd per attach (uniform
    # lifecycle), but with enabled=False it spawns no thread / does no writes.
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert len(st["notice_starts"]) == 1               # handle built per attach (no-op when disabled)
    assert st["notice_starts"][0]["enabled"] is False  # default OFF
    assert st["notice_starts"][0]["mem"] == {"pid": 100}
    assert st["notice_stops"] == 1                      # stop_and_join'd once (no-op handle)


def test_run_no_notice_disables_scanner(run_env):  # noqa: F811
    st = run_env["state"]
    st["serve_script"] = [_user_quit]

    cli.run(hooks="dialogue", duration=0.0, patch=True, notice=False)

    assert len(st["notice_starts"]) == 1
    assert st["notice_starts"][0]["enabled"] is False  # disabled
    assert st["notice_stops"] == 1                      # still stop_and_join'd (no-op handle)


def test_run_notice_started_and_stopped_per_attach(run_env):  # noqa: F811
    from test_lifecycle import _gone

    st = run_env["state"]
    st["serve_script"] = [_gone, _user_quit]  # attach, game-gone, re-attach, user quit

    cli.run(hooks="dialogue", duration=0.0, patch=True)

    assert len(st["notice_starts"]) == 2               # one per attach
    assert st["notice_starts"][0]["mem"] == {"pid": 100}
    assert st["notice_starts"][1]["mem"] == {"pid": 200}
    assert st["notice_stops"] == 2                      # each attach's scanner stopped+joined once
