"""Tests for the x86 detour assembler (offline — no game needed)."""

from __future__ import annotations

import struct

from dqxclarity.process import detour


def _decode_rel32(buf: bytes, e9_off: int) -> int:
    """Return the absolute target of an E9 jmp at ``e9_off`` within buf, given buf base addr 0."""
    rel = struct.unpack("<i", buf[e9_off + 1 : e9_off + 5])[0]
    return e9_off + 5 + rel  # target relative to buf base


def test_make_detour_jmp():
    func, cave = 0x00500000, 0x02060000
    jmp = detour.make_detour_jmp(func, cave, 6)
    assert len(jmp) == 6
    assert jmp[0] == 0xE9
    assert jmp[5] == 0x90  # NOP pad
    rel = struct.unpack("<i", jmp[1:5])[0]
    assert func + 5 + rel == cave  # jumps to the cave


def test_capture_shellcode_structure_and_jumpback():
    func = 0x00500000
    stolen = bytes.fromhex("558BEC568BF1")  # the 6 dialogue prologue bytes
    code_addr = 0x02060104  # cave + 4 + 256
    head_addr = 0x02060000
    ring_addr = 0x02060004
    sc = detour.build_capture_shellcode(
        code_addr=code_addr, head_addr=head_addr, ring_addr=ring_addr,
        func_addr=func, stolen=stolen,
    )
    assert sc[0] == 0x60 and sc[1] == 0x9C  # pushad; pushfd
    assert stolen in sc  # original instructions preserved
    # embedded data addresses
    assert struct.pack("<I", head_addr) in sc
    assert struct.pack("<I", ring_addr) in sc
    # final instruction is the jmp back to func + len(stolen)
    e9_off = len(sc) - 5
    assert sc[e9_off] == 0xE9
    abs_target_rel_to_code = code_addr + _decode_rel32(sc, e9_off)
    assert abs_target_rel_to_code == func + len(stolen)


def test_blocking_shellcode_structure():
    func = 0x00500000
    stolen = bytes.fromhex("558BEC568BF1")
    code_addr = 0x02060010
    state_addr, slot_addr = 0x02060000, 0x02060004
    sc = detour.build_blocking_shellcode(
        code_addr=code_addr, state_addr=state_addr, slot_addr=slot_addr,
        func_addr=func, stolen=stolen, timeout=12345,
    )
    assert sc[0] == 0x60 and sc[1] == 0x9C  # pushad; pushfd
    assert b"\xa3" + struct.pack("<I", slot_addr) in sc  # mov [SLOT], eax
    assert struct.pack("<I", state_addr) in sc
    assert b"\xf3\x90" in sc  # pause in the spin loop
    assert stolen in sc
    # ends with jmp back to func + len(stolen)
    e9_off = len(sc) - 5
    assert sc[e9_off] == 0xE9
    assert code_addr + _decode_rel32(sc, e9_off) == func + len(stolen)
    # je .done is +5; jnz .wait is -14 (the documented offsets)
    assert b"\x74\x05" in sc and b"\x75\xf2" in sc


def test_arg_offset_is_40():
    # mov eax,[esp+0x28] -> reads arg0 at +40 (pushad 32 + pushfd 4 + retaddr 4)
    sc = detour.build_capture_shellcode(
        code_addr=0x1000, head_addr=0x10, ring_addr=0x14,
        func_addr=0x2000, stolen=b"\x55",
    )
    assert b"\x8b\x44\x24\x28" in sc  # mov eax,[esp+0x28]


def test_blocking_shellcode_default_capture_is_arg0():
    sc = detour.build_blocking_shellcode(
        code_addr=0x1000, state_addr=0x10, slot_addr=0x14, func_addr=0x2000, stolen=b"\x55",
    )
    assert sc[2:6] == detour.CAPTURE_ARG0 == b"\x8b\x44\x24\x28"  # mov eax,[esp+0x28]


def test_blocking_shellcode_custom_capture_threaded_and_jumpback_recomputed():
    # The walkthrough capture: mov eax,[esp+0x20]; add eax,0xEC  (saved eax = obj ptr, +text offset)
    from dqxclarity.process import signatures as sig

    cap = sig.WALKTHROUGH_CAPTURE
    assert cap == b"\x8b\x44\x24\x20\x05\xec\x00\x00\x00"
    assert cap[:4] == b"\x8b\x44\x24\x20"  # mov eax,[esp+0x20] = saved eax (object ptr) after pushad+pushfd
    func = 0x006E13CD
    stolen = bytes.fromhex("8db8ec000000")  # lea edi,[eax+0xEC]
    code_addr = 0x02060010
    sc = detour.build_blocking_shellcode(
        code_addr=code_addr, state_addr=0x02060000, slot_addr=0x02060004,
        func_addr=func, stolen=stolen, capture=cap,
    )
    assert sc[0] == 0x60 and sc[1] == 0x9C        # pushad; pushfd
    assert sc[2 : 2 + len(cap)] == cap            # custom capture spliced in right after pushfd
    assert stolen in sc                           # original lea preserved
    assert b"\xf3\x90" in sc                      # spin loop intact
    assert b"\x74\x05" in sc and b"\x75\xf2" in sc  # self-relative jumps unchanged by capture len
    # jmp-back still lands at func+len(stolen) despite the longer (9-byte) capture
    e9_off = len(sc) - 5
    assert sc[e9_off] == 0xE9
    assert code_addr + _decode_rel32(sc, e9_off) == func + len(stolen)
