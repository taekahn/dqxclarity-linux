"""Tests for BlockingHook.serve_once — the write-back path that guards invariant I1
(never overflow/corrupt the game's text buffer) and I4 (always release the game thread)."""

from __future__ import annotations

import struct

import pytest

from dqxclarity.process.detour import STATE_DONE, STATE_REQUEST, BlockingHook

STATE = 0x10
SLOT = 0x14
PTR = 0x1000


class FakeMem:
    """Minimal address-space stub: u32 registers + byte buffers + a write log."""

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


def _hook() -> BlockingHook:
    return BlockingHook(func_addr=0x400000, cave_addr=0, state_addr=STATE, slot_addr=SLOT,
                        code_addr=0, saved_bytes=b"")


def _mem(buffer: bytes) -> FakeMem:
    m = FakeMem()
    m.u32[STATE] = STATE_REQUEST
    m.u32[SLOT] = PTR
    m.buffers[PTR] = buffer
    return m


def _writes_to(mem: FakeMem, addr: int) -> list[bytes]:
    return [d for a, d in mem.writes if a == addr]


def test_serve_once_writes_en_and_releases():
    ja = "テスト".encode()  # 9 bytes
    mem = _mem(ja + b"\x00" + b"\x00" * 200)  # JA + NUL + slack
    _hook().serve_once(mem, lambda j: "Test")
    w = _writes_to(mem, PTR)
    assert w and w[-1].startswith(b"Test\x00")
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # released


def test_serve_once_zeros_ja_tail():
    ja = "テストです".encode()  # 15 bytes; EN is shorter -> tail must be zeroed
    mem = _mem(ja + b"\x00" + b"\x00" * 50)
    _hook().serve_once(mem, lambda j: "Hi")
    data = _writes_to(mem, PTR)[-1]
    assert data.startswith(b"Hi\x00")
    assert len(data) >= len(ja) + 1 and set(data[3:]) == {0}  # padded over the old JA span


def test_serve_once_skips_when_too_long_for_capacity():
    ja = "あ".encode()  # 3 bytes
    mem = _mem(ja + b"\x00" + b"\x01" * 100)  # cap = ja_len+1 = 4 (next byte non-zero)
    _hook().serve_once(mem, lambda j: "way too long to fit in the buffer")
    assert _writes_to(mem, PTR) == []  # no write — display-safe (I1)
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # still released


def test_serve_once_releases_even_when_translate_raises():
    mem = _mem("テスト".encode() + b"\x00" * 50)

    def boom(_):
        raise RuntimeError("provider exploded")

    _hook().serve_once(mem, boom)  # must NOT propagate
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)  # released (I2/I4)
    assert _writes_to(mem, PTR) == []  # nothing written on failure


def test_serve_once_noop_when_not_requested():
    mem = _mem("テスト".encode() + b"\x00" * 50)
    mem.u32[STATE] = STATE_DONE  # no pending request
    assert _hook().serve_once(mem, lambda j: "x") is None
    assert mem.writes == []


def test_serve_once_does_not_write_when_unchanged():
    mem = _mem("テスト".encode() + b"\x00" * 50)
    _hook().serve_once(mem, lambda j: j)  # translate returns the JA unchanged
    assert _writes_to(mem, PTR) == []  # nothing to do
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)


def test_serve_once_multi_field():
    """A struct hook (fields at offsets) translates and writes back each field."""
    base = PTR
    mem = FakeMem()
    mem.u32[STATE] = STATE_REQUEST
    mem.u32[SLOT] = base
    mem.buffers[base] = "あ".encode() + b"\x00" + b"\x00" * 50  # field at offset 0
    mem.buffers[base + 100] = "い".encode() + b"\x00" + b"\x00" * 50  # field at offset 100
    hook = BlockingHook(0x400000, 0, STATE, SLOT, 0, b"", fields=((0, 80), (100, 80)))
    hook.serve_once(mem, lambda j: "X" if j == "あ" else "Y")
    assert _writes_to(mem, base)[-1].startswith(b"X\x00")
    assert _writes_to(mem, base + 100)[-1].startswith(b"Y\x00")
    assert _writes_to(mem, STATE)[-1] == struct.pack("<I", STATE_DONE)


@pytest.mark.parametrize("en_len", [1, 50, 200])
def test_serve_once_never_exceeds_capacity(en_len):
    """I1 property: the written bytes never exceed the buffer's zero-run capacity."""
    ja = "ようこそ".encode()  # 12 bytes
    slack = 100
    mem = _mem(ja + b"\x00" * slack + b"\xff" * 10)  # cap = 12 + slack (zeros) ... + NUL handling
    cap = len(ja) + slack  # ja_len + trailing zero run
    _hook().serve_once(mem, lambda j: "E" * en_len)
    w = _writes_to(mem, PTR)
    if w:
        assert len(w[-1]) <= cap + 1  # +1 for the included terminator budget
