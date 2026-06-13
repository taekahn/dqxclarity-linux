"""Tests for the `send-text` chat-injection command (#33) — offline, no game, no network.

The full mechanism was reverse-engineered + validated LIVE by hand; these tests pin the proven
flow against a FAKE mem backed by an in-memory byte buffer laid out exactly as the game stores the
chat input:

    [capacity:u32][40 44 D8 02][utf-8 text][00]

i.e. the CHAT_STRING_HEADER (``40 44 D8 02``) immediately precedes the null-terminated UTF-8 text,
with the u32 capacity immediately before the header. For a header at H: text_addr = H+4 and capacity
is read at H-4. The chat box is reallocated each open with typically 2-3 live copies of different
capacities (e.g. 192/128/32), so the user types a short ASCII SENTINEL the tool scans for
(``CHAT_STRING_HEADER + sentinel``); we overwrite every copy that FITS and skip any too small.

These exercise the pure helper ``cli._inject_chat_text`` directly (writes the UTF-8 + NUL into copies
that fit, skips/never-overflows the too-small one, guards against clobbering a stale buffer) plus the
thin `send-text` command's exit codes / messages (sentinel not found, game not running, too long,
non-ASCII sentinel).
"""

from __future__ import annotations

import struct

import pytest
import typer

from dqxclarity import cli
from dqxclarity.process.signatures import CHAT_STRING_HEADER


class FakeChatMem:
    """In-memory byte buffer exposing the LinuxProcessMemory surface ``_inject_chat_text`` uses.

    Build it from a list of (capacity, text) copies; each is laid out as
    ``[capacity u32][40 44 D8 02][utf-8 text][00]`` and padded out to ``capacity`` bytes of text
    space, with junk between copies so pattern_scan finds each header independently.
    """

    def __init__(
        self, copies: list[tuple[int, str]], *, base: int = 0x200000, edit_control_for: int | None = None
    ) -> None:
        self.base = base
        self.buf = bytearray(b"\x00" * 64)  # leading junk so addresses are non-zero/realistic
        self.text_addrs: list[int] = []
        self.caps: list[int] = []
        for cap, text in copies:
            data = text.encode("utf-8") + b"\x00"
            assert len(data) <= cap, "test copy text must fit its declared capacity"
            data += b"\x00" * (cap - len(data))
            self.caps.append(cap)
            self.buf += struct.pack("<I", cap)          # [capacity:u32]
            self.buf += CHAT_STRING_HEADER              # [40 44 D8 02]
            self.text_addrs.append(self.base + len(self.buf))
            self.buf += data                            # [utf-8 text][00][pad...]
            self.buf += b"\x5a" * 16                    # junk gap between copies
        # Optional edit-control object for one copy: a location R holding u32 == that copy's text_addr,
        # with the 0xFFFFFFFF anchor at R+40 and length/caret slots at R+48/R+52 (the live-validated
        # layout). _set_chat_length reverse-scans for the text_addr and writes those slots.
        self.edit_ctrl_addr: int | None = None
        if edit_control_for is not None:
            self.edit_ctrl_addr = self.base + len(self.buf)
            block = bytearray(struct.pack("<I", self.text_addrs[edit_control_for]))  # +0: ptr to text
            block += b"\x11" * (40 - len(block))                                     # +4..+40 junk
            block += struct.pack("<I", 0xFFFFFFFF)                                   # +40: anchor
            block += struct.pack("<I", 0x22222222)                                   # +44: unrelated
            block += struct.pack("<I", 0)                                            # +48: length slot
            block += struct.pack("<I", 0)                                            # +52: caret slot
            self.buf += block

    def _off(self, addr: int) -> int:
        return addr - self.base

    def pattern_scan(self, pattern, *, data_only=False, return_multiple=True, limit=None):
        import re

        rx = re.compile(pattern, re.DOTALL)
        addrs = [self.base + m.start() for m in rx.finditer(bytes(self.buf))]
        if not return_multiple:
            return addrs[0] if addrs else None
        return addrs

    def read(self, addr: int, size: int) -> bytes:
        off = self._off(addr)
        if off < 0:
            return b""
        return bytes(self.buf[off:off + size])

    def read_u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def read_cstring(self, addr: int, max_len: int = 512, encoding: str = "utf-8") -> str:
        raw = self.read(addr, max_len)
        end = raw.find(b"\x00")
        if end != -1:
            raw = raw[:end]
        return raw.decode(encoding, "replace")

    def write(self, addr: int, data: bytes) -> int:
        off = self._off(addr)
        self.buf[off:off + len(data)] = data
        return len(data)

    def write_cstring(self, addr: int, text: str, *, max_bytes: int, encoding: str = "utf-8") -> bool:
        data = text.encode(encoding, "replace") + b"\x00"
        if len(data) > max_bytes:
            return False
        data += b"\x00" * (max_bytes - len(data))
        self.write(addr, data)
        return True

    def read_text(self, idx: int) -> str:
        return self.read_cstring(self.text_addrs[idx])


# =============================================================================================== #
# _inject_chat_text — the pure, game-free core                                                    #
# =============================================================================================== #


def test_inject_writes_utf8_into_every_copy_that_fits():
    # Three live copies of the sentinel "qzx" at 192/128/32-byte capacities (the real shape).
    mem = FakeChatMem([(192, "qzx"), (128, "qzx"), (32, "qzx")])
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "こんにちは", "qzx")

    # "こんにちは" is 15 UTF-8 bytes + NUL = 16, fits all three.
    assert written == 3
    assert skipped == []
    assert max_cap == 192
    for idx in range(3):
        assert mem.read_text(idx) == "こんにちは"


def test_inject_skips_too_small_copy_and_never_overflows():
    needed = len("テスト送信".encode("utf-8")) + 1  # 15 bytes + NUL = 16
    # One copy fits (192), one is too small (8 bytes can't hold 16) -> skipped, never overflowed.
    mem = FakeChatMem([(192, "qzx"), (8, "qzx")])
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "テスト送信", "qzx")

    assert written == 1
    assert max_cap == 192
    assert skipped == [(8, needed)]
    # The big copy got the text; the small copy is UNTOUCHED (still the sentinel) — no overflow.
    assert mem.read_text(0) == "テスト送信"
    assert mem.read_text(1) == "qzx"
    # And the small copy's text never grew past its 8-byte capacity (no clobber of trailing junk).
    small_off = mem._off(mem.text_addrs[1])
    assert mem.buf[small_off + 8:small_off + 8 + 3] == b"\x5a\x5a\x5a"  # junk gap intact


def test_inject_exact_fit_boundary_writes():
    # Capacity == len(utf8)+1 must fit (the <= boundary): "ab" -> 2 + NUL = 3 bytes, cap 3.
    mem = FakeChatMem([(3, "ab")])
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "ab", "ab")
    assert written == 1 and skipped == [] and max_cap == 3
    assert mem.read_text(0) == "ab"


def test_inject_one_byte_short_is_skipped():
    # cap 2 can hold only "a"+NUL; "ab" needs 3 -> too small -> skipped, not written.
    mem = FakeChatMem([(2, "a")])
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "ab", "a")
    assert written == 0
    assert max_cap == 0
    assert skipped == [(2, 3)]
    assert mem.read_text(0) == "a"  # untouched


def test_inject_no_hits_returns_zero():
    mem = FakeChatMem([(192, "qzx")])
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "x", "ZZZ")  # sentinel ZZZ absent
    assert written == 0 and skipped == [] and max_cap == 0


def test_inject_guards_against_stale_buffer_that_no_longer_holds_sentinel(monkeypatch):
    # A header+sentinel match whose CURRENT text no longer starts with the sentinel must be skipped
    # (never clobbered) — guards against overwriting an unrelated buffer that matched transiently.
    mem = FakeChatMem([(192, "qzx")])
    real_read_cstring = mem.read_cstring

    def lying_read_cstring(addr, max_len=512, encoding="utf-8"):
        if addr == mem.text_addrs[0]:
            return "somethingelse"  # pretend the buffer changed out from under us
        return real_read_cstring(addr, max_len, encoding)

    monkeypatch.setattr(mem, "read_cstring", lying_read_cstring)
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "X", "qzx")
    assert written == 0 and skipped == []
    # Read the RAW buffer (not the lying read_cstring) to confirm it was never written.
    off = mem._off(mem.text_addrs[0])
    assert bytes(mem.buf[off:off + 3]) == b"qzx"  # untouched


# =============================================================================================== #
# send-text command — exit codes + messages                                                       #
# =============================================================================================== #


def test_send_text_game_not_running_exits_1(monkeypatch):
    monkeypatch.setattr(cli, "find_game_pid", lambda: None)
    with pytest.raises(typer.Exit) as ei:
        cli.send_text(text="hi", sentinel="qzx")
    assert ei.value.exit_code == 1


def test_send_text_non_ascii_sentinel_exits_1(monkeypatch):
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    with pytest.raises(typer.Exit) as ei:
        cli.send_text(text="hi", sentinel="あ")  # not typeable without an IME
    assert ei.value.exit_code == 1


def test_send_text_sentinel_not_found_exits_1(monkeypatch):
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    mem = FakeChatMem([(192, "qzx")])
    # LinuxProcessMemory is imported lazily inside the command; patch at its source module.
    import dqxclarity.process.memory_linux as mlin
    monkeypatch.setattr(mlin, "LinuxProcessMemory", lambda pid: mem)

    with pytest.raises(typer.Exit) as ei:
        cli.send_text(text="hi", sentinel="NOPE")  # absent sentinel -> zero hits
    assert ei.value.exit_code == 1


def test_send_text_too_long_writes_nothing_exits_1(monkeypatch):
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    # Only a tiny 8-byte copy exists; any reasonable text overflows it -> nothing written -> exit 1.
    mem = FakeChatMem([(8, "qzx")])
    import dqxclarity.process.memory_linux as mlin
    monkeypatch.setattr(mlin, "LinuxProcessMemory", lambda pid: mem)

    with pytest.raises(typer.Exit) as ei:
        cli.send_text(text="この文章は長すぎます", sentinel="qzx")
    assert ei.value.exit_code == 1
    assert mem.read_text(0) == "qzx"  # untouched, never overflowed


def test_send_text_success_writes_and_returns_none(monkeypatch):
    monkeypatch.setattr(cli, "find_game_pid", lambda: 1234)
    mem = FakeChatMem([(192, "qzx"), (128, "qzx")])
    import dqxclarity.process.memory_linux as mlin
    monkeypatch.setattr(mlin, "LinuxProcessMemory", lambda pid: mem)

    # A successful inject prints a result and returns normally (no Exit raised).
    assert cli.send_text(text="やあ", sentinel="qzx") is None
    assert mem.read_text(0) == "やあ"
    assert mem.read_text(1) == "やあ"


# =============================================================================================== #
# edit-control LENGTH/caret setting (so the whole message sends, not just the sentinel's length)   #
# =============================================================================================== #


def test_inject_sets_edit_control_length_and_caret():
    import struct as _s
    from dqxclarity.process import signatures as sig

    # Main 192 copy has an edit-control; the helper must set its LENGTH + CARET to len(utf8).
    mem = FakeChatMem([(192, "qzx")], edit_control_for=0)
    msg = "ごめんねなの！"
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, msg, "qzx")
    assert written == 1 and length_set == 1
    assert mem.read_text(0) == msg
    R = mem.edit_ctrl_addr
    L = len(msg.encode("utf-8"))
    assert mem.read_u32(R + sig.CHAT_EDIT_LENGTH_OFFSET) == L  # send reads this many bytes
    assert mem.read_u32(R + sig.CHAT_EDIT_CARET_OFFSET) == L   # caret moved to end


def test_inject_no_edit_control_reports_length_unset():
    # Render copies with no edit-control: text is written but length can't be set (length_set == 0).
    mem = FakeChatMem([(192, "qzx")])  # no edit_control_for
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "テスト", "qzx")
    assert written == 1 and length_set == 0


def test_inject_sentinel_with_regex_metachars_is_escaped():
    # A sentinel containing regex metacharacters ('.', '[') must be matched LITERALLY, not as a regex.
    mem = FakeChatMem([(192, "a.[b")])
    written, skipped, max_cap, length_set = cli._inject_chat_text(mem, "ありがとう", "a.[b")
    assert written == 1
    assert mem.read_text(0) == "ありがとう"
