"""Native x86 (32-bit) detour hooking for the WOW64 game — no Frida, no ptrace.

Because Wine runs the game's x86 code natively, a trampoline we inject executes on the game's
own thread. We:

  1. find the target function by its prologue signature,
  2. find a **code cave** — a run of zero bytes in an existing `rwxp` region of the game image —
     to hold our shellcode + a small ring buffer (writable+executable, so no allocation needed),
  3. write our shellcode into the cave,
  4. overwrite the function's first instructions with a `jmp` to the cave (saving the originals),
  5. the shellcode records the argument we want (e.g. the dialogue text pointer) into the ring
     buffer, runs the stolen instructions, and jumps back.

Python then polls the ring buffer via `process_vm_readv`. Capture-only for now (async); blocking
write-back (spin on a shared flag) is the next iteration.

RISK: this writes executable code into a live process. A bug crashes the game. Install saves the
original bytes and `restore()` puts them back. Use on a disposable/backup install first.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .memory_linux import LinuxProcessMemory

RING_BYTES = 256  # 64 u32 slots; head is a byte offset masked to this (power of two)
RING_MASK = RING_BYTES - 1
_READ_WINDOW = 2048  # bytes read around a dialogue buffer; also the hard cap on write-back size

# Spin-loop iteration ceiling for the blocking/return handshakes: how many pause-decrement spins
# the game thread does waiting for Python to write EN before it gives up and renders the JA.
# This is a FLOOR-CONSTRAINED value — it must stay large enough to cover the worst-case synchronous
# MT translation time (a slow Google call can take up to ~1s) so a legitimate first-view
# translation is not cut off and shown as Japanese.
#
# Floor reasoning: the spin body is cmp/je/PAUSE/dec/jnz; PAUSE is ~10-40 cycles, so on a 3+ GHz
# CPU at ~10 cycles/iter, 300M ≈ ~1.0s — just enough to cover a worst-case ~1s synchronous Google
# MT call. 150M ≈ ~0.5s on fast hardware, which is BELOW that worst case → a legit first-view
# translation would be cut off and shown as Japanese (a regression). Do NOT reduce below ~250M.
# The orphaned-hook stall (a detour the game still has after an unclean exit) is bounded by the
# hook journal + signal handlers, NOT by trimming this timeout — so keep this conservative.
SPIN_TIMEOUT = 300_000_000


def find_code_cave(
    mem: LinuxProcessMemory, size: int, *, module: str = "DQXGame.exe"
) -> int | None:
    """Find a run of >= ``size`` zero bytes in an rwxp section **of the game image**.

    Restricted to regions file-backed by ``module`` so we use the game's own static
    writable+executable sections (stable padding), never an anonymous low Wine page.
    """
    for r in mem.regions():
        if not r.perms.startswith("rwx") or r.start > 0x7FFFFFFF:
            continue
        if not r.path.endswith(module):
            continue
        try:
            buf = mem.read(r.start, r.size)
        except OSError:
            continue
        run = 0
        for i, b in enumerate(buf):
            if b == 0:
                run += 1
                if run >= size:
                    # Return an address a little inside the run (skip a leading guard byte).
                    return r.start + (i - run + 1)
            else:
                run = 0
    return None


def _rel32(src_after: int, target: int) -> bytes:
    return struct.pack("<i", target - src_after)


def make_detour_jmp(func_addr: int, cave_code_addr: int, stolen_len: int) -> bytes:
    """5-byte `jmp cave` + NOP padding to fill ``stolen_len`` bytes at the function entry."""
    jmp = b"\xe9" + _rel32(func_addr + 5, cave_code_addr)
    return jmp + b"\x90" * (stolen_len - len(jmp))


def build_capture_shellcode(
    *, code_addr: int, head_addr: int, ring_addr: int, func_addr: int, stolen: bytes
) -> bytes:
    """Shellcode that records arg0 (text ptr) into the ring buffer, then runs ``stolen`` + returns.

    ``stolen`` are the original instruction bytes overwritten at the function entry (must be whole
    instructions; for the dialogue function: push ebp; mov ebp,esp; push esi; mov esi,ecx = 6 b).
    arg0 sits at [esp+0x28] after pushad(32)+pushfd(4)+retaddr(4) = +40 from the saved esp.
    """
    sc = bytearray()
    sc += b"\x60"                                   # pushad
    sc += b"\x9c"                                   # pushfd
    sc += b"\x8b\x44\x24\x28"                       # mov eax, [esp+0x28]   ; arg0 = text ptr
    sc += b"\x8b\x0d" + struct.pack("<I", head_addr)        # mov ecx, [head]
    sc += b"\x89\x81" + struct.pack("<I", ring_addr)        # mov [ecx+ring], eax
    sc += b"\x83\xc1\x04"                           # add ecx, 4
    sc += b"\x81\xe1" + struct.pack("<I", RING_MASK)        # and ecx, RING_MASK
    sc += b"\x89\x0d" + struct.pack("<I", head_addr)        # mov [head], ecx
    sc += b"\x9d"                                   # popfd
    sc += b"\x61"                                   # popad
    sc += stolen                                    # original prologue instructions
    jmp_at = code_addr + len(sc)
    sc += b"\xe9" + _rel32(jmp_at + 5, func_addr + len(stolen))  # jmp back to func+stolen_len
    return bytes(sc)


# Default capture: arg0 (the text pointer) sits at [esp+0x28] after pushad(32)+pushfd(4)+retaddr(4),
# for a hook placed at a __thiscall/__cdecl function PROLOGUE. The capture sequence must leave the
# text-buffer address in eax. Surfaces that capture differently (e.g. a return value at a call site)
# pass their own sequence; see CAPTURE_RETURN_EAX_PLUS in signatures for the walkthrough surface.
CAPTURE_ARG0 = b"\x8b\x44\x24\x28"                      # mov eax,[esp+0x28]
# 3rd stack arg (a4) of a __thiscall: at a prologue, after pushad(32)+pushfd(4)+retaddr(4)=+40,
# arg0 is at +0x28, arg1 at +0x2C, arg2 (a4) at +0x30. The corner-text surface's text pointer is
# this arg, so its capture loads [esp+0x30] into eax.
CAPTURE_ARG2 = b"\x8b\x44\x24\x30"                      # mov eax,[esp+0x30] = 3rd stack arg (a4)


def build_blocking_shellcode(
    *, code_addr: int, state_addr: int, slot_addr: int, func_addr: int, stolen: bytes,
    timeout: int = SPIN_TIMEOUT, capture: bytes = CAPTURE_ARG0,
) -> bytes:
    """Blocking detour: record the text ptr, signal Python, spin until it writes EN, then continue.

    Handshake via shared dwords: STATE 0=idle,1=request,2=done. The shellcode runs ``capture`` to
    load the text-buffer address into eax, sets STATE=1 with that address in SLOT, spins (with a
    bounded counter so a dead Python can't freeze the game) until Python sets STATE=2, then resets
    STATE=0 and runs the stolen instructions. ``capture`` defaults to reading arg0 at a function
    prologue; a different surface (e.g. a call-site return value) supplies its own sequence. Only
    this one instruction varies — the spin loop's relative jumps are self-contained, and the final
    jump-back is recomputed from len(sc), so changing ``capture``'s length stays correct.
    """
    # NOTE: the byte offsets below assume the DEFAULT 4-byte CAPTURE_ARG0. A longer `capture`
    # (e.g. the 9-byte walkthrough one) shifts everything after the capture by the same delta —
    # which is harmless: the spin-loop jumps are self-relative and the final jmp uses len(sc).
    sc = bytearray()
    sc += b"\x60"                                       # 0  pushad
    sc += b"\x9c"                                       # 1  pushfd
    sc += capture                                       # 2  load text-buffer addr -> eax
    sc += b"\xa3" + struct.pack("<I", slot_addr)        #    mov [SLOT], eax
    sc += b"\xc7\x05" + struct.pack("<I", state_addr) + b"\x01\x00\x00\x00"   # 11 mov [STATE],1
    sc += b"\xb9" + struct.pack("<I", timeout)          # 21 mov ecx, TIMEOUT
    # .wait (offset 26):
    sc += b"\x83\x3d" + struct.pack("<I", state_addr) + b"\x02"   # 26 cmp dword[STATE],2
    sc += b"\x74\x05"                                   # 33 je .done (+5 -> 40)
    sc += b"\xf3\x90"                                   # 35 pause
    sc += b"\x49"                                       # 37 dec ecx
    sc += b"\x75\xf2"                                   # 38 jnz .wait (-14 -> 26)
    # .done (offset 40):
    sc += b"\xc7\x05" + struct.pack("<I", state_addr) + b"\x00\x00\x00\x00"   # 40 mov [STATE],0
    sc += b"\x9d"                                       # 50 popfd
    sc += b"\x61"                                       # 51 popad
    sc += stolen                                        # 52 original prologue
    jmp_at = code_addr + len(sc)
    sc += b"\xe9" + _rel32(jmp_at + 5, func_addr + len(stolen))   # jmp back
    return bytes(sc)


# Handshake states.
STATE_IDLE, STATE_REQUEST, STATE_DONE = 0, 1, 2


# ---- return hook (read the result AFTER the function returns) ---------------------------------- #
#
# Some surfaces (network_text "Story So Far" / NPC names / quest template strings) produce their text
# only as the function RETURNS true — there's nothing to read at the prologue. We mirror upstream's
# Frida `Interceptor.attach({onLeave})`: at the prologue we record the original return address and the
# context arg onto a per-call **shadow stack**, then hijack the on-stack return address so the function
# `ret`s into our EXIT shellcode instead. The exit code pops the shadow frame, and (only if eax==1)
# runs the blocking handshake (signal Python, spin until it writes EN), then returns to the real caller.
#
# A shadow stack (not a single slot) is required because the hooked function can recurse/reenter on the
# same thread: each ENTRY pushes a frame, each EXIT pops the matching one, so nested calls don't clobber
# each other's saved return address.
FRAMES = 64          # shadow-stack depth (frames); each frame is 8 bytes (retaddr u32 + ctx u32)
SHADOW_BYTES = FRAMES * 8


def build_return_shellcode(
    *, entry_code: int, exit_code: int, state_addr: int, ctx_slot: int, sp_addr: int,
    shadow: int, func_addr: int, stolen: bytes, timeout: int = SPIN_TIMEOUT,
) -> tuple[bytes, bytes]:
    """Assemble the (entry, exit) shellcode for a return hook. See module notes above.

    ENTRY (at the prologue): save regs/flags, push {original retaddr, ctx a1} onto the shadow stack,
    bump SP, overwrite the on-stack return address with ``exit_code``, restore, run the stolen
    prologue, jmp back to func+len(stolen).

    EXIT (entered when the function `ret`s here; eax = return value, esp at the caller's args): pop the
    shadow frame; only if eax==1, signal Python (STATE=request, ctx in CTX_SLOT) and spin until DONE;
    then return to the real caller. esp + flags are preserved exactly across the exit hook (I1).
    """
    # ---- ENTRY ----
    e = bytearray()
    e += b"\x60"                                                    # pushad
    e += b"\x9c"                                                    # pushfd
    e += b"\xa1" + struct.pack("<I", sp_addr)                       # mov eax,[sp_addr]   (SP byte off)
    e += b"\x89\xc1"                                                # mov ecx,eax         (old SP)
    e += b"\x83\xc0\x08"                                            # add eax,8
    e += b"\xa3" + struct.pack("<I", sp_addr)                       # mov [sp_addr],eax   (SP += 8)
    e += b"\x8b\x54\x24\x24"                                        # mov edx,[esp+0x24]  (orig retaddr)
    e += b"\x89\x91" + struct.pack("<I", shadow)                    # mov [ecx+shadow],edx (frame.ret)
    e += b"\x8b\x54\x24\x28"                                        # mov edx,[esp+0x28]  (a1 ctx)
    e += b"\x89\x91" + struct.pack("<I", shadow + 4)                # mov [ecx+shadow+4],edx (frame.ctx)
    e += b"\xc7\x44\x24\x24" + struct.pack("<I", exit_code)         # mov [esp+0x24],exit_code (hijack)
    e += b"\x9d"                                                    # popfd
    e += b"\x61"                                                    # popad
    e += stolen                                                     # original prologue
    jmp_at = entry_code + len(e)
    e += b"\xe9" + _rel32(jmp_at + 5, func_addr + len(stolen))      # jmp back to func+stolen_len

    # ---- EXIT ----
    x = bytearray()
    x += b"\x50"                                                    # push eax            (save retval)
    x += b"\x9c"                                                    # pushfd              (save flags)
    x += b"\x8b\x0d" + struct.pack("<I", sp_addr)                   # mov ecx,[sp_addr]
    x += b"\x83\xe9\x08"                                            # sub ecx,8
    x += b"\x89\x0d" + struct.pack("<I", sp_addr)                   # mov [sp_addr],ecx   (SP -= 8)
    x += b"\x8b\x91" + struct.pack("<I", shadow)                    # mov edx,[ecx+shadow] (retaddr)
    x += b"\x8b\x81" + struct.pack("<I", shadow + 4)                # mov eax,[ecx+shadow+4] (ctx)
    x += b"\x52"                                                    # push edx            (save retaddr)
    x += b"\x8b\x4c\x24\x08"                                        # mov ecx,[esp+8]     (saved retval)
    x += b"\x83\xf9\x01"                                            # cmp ecx,1
    # jne .skip — displacement = bytes from here to the `5A` (pop edx) below. Assembled + verified to
    # be 44 (0x2C); recomputed from the actual block so it can never silently drift.
    jne_at = len(x)
    x += b"\x75\x00"                                                # jne .skip (disp patched below)
    after_jne = len(x)
    x += b"\xa3" + struct.pack("<I", ctx_slot)                      # mov [ctx_slot],eax  (ctx for Py)
    x += b"\xc7\x05" + struct.pack("<I", state_addr) + b"\x01\x00\x00\x00"   # mov [STATE],1 (request)
    x += b"\xb9" + struct.pack("<I", timeout)                       # mov ecx,timeout
    # .wait:
    x += b"\x83\x3d" + struct.pack("<I", state_addr) + b"\x02"      # cmp dword[STATE],2
    x += b"\x74\x05"                                                # je .done (+5)
    x += b"\xf3\x90"                                                # pause
    x += b"\x49"                                                    # dec ecx
    x += b"\x75\xf2"                                                # jnz .wait (-14)
    # .done:
    x += b"\xc7\x05" + struct.pack("<I", state_addr) + b"\x00\x00\x00\x00"   # mov [STATE],0 (idle)
    # .skip:
    skip_at = len(x)
    x[jne_at + 1] = skip_at - after_jne                            # patch jne disp to the real .skip
    x += b"\x5a"                                                    # pop edx             (retaddr)
    x += b"\x9d"                                                    # popfd               (restore flags)
    x += b"\x58"                                                    # pop eax             (restore retval)
    x += b"\xff\xe2"                                                # jmp edx             (real return)

    return bytes(e), bytes(x)


@dataclass
class ReturnHook:
    func_addr: int
    cave: int
    state_addr: int
    ctx_slot: int
    saved_bytes: bytes
    # (struct offset, max writable bytes) — kept for parity with BlockingHook; the network surface
    # reads len/end/category from the context directly, so the default single entry is unused here.
    fields: tuple[tuple[int, int], ...] = ((0, 1 << 20),)
    requests: int = field(default=0, init=False)  # game-side requests serviced (profiling/hot-hook)

    def serve_once(self, mem: LinuxProcessMemory, translate) -> str | None:
        """If a return is pending, translate the produced string and write EN back within ``length``.

        ``translate`` takes (ja, category). Reads the context captured by the exit shellcode: the
        string length @ ctx+0x10, the end-of-buffer addr @ ctx+0x18 (start = end - length, NOT
        null-terminated), and the category string ptr @ ctx+0x1c. Writes a null-terminated EN at
        ``start`` ONLY if it fits within ``length`` (invariant I1 — never exceed the buffer span);
        zero-pads the remainder so no stale JA tail remains. ALWAYS releases the game thread (STATE
        -> DONE) in a finally, even on error, so the spinning thread never hangs. Returns the JA
        string handled (for logging), else None.
        """
        if mem.read_u32(self.state_addr) != STATE_REQUEST:
            return None
        self.requests += 1  # a real game-side call is being serviced (hot-hook profiling)
        ja: str | None = None
        try:
            ctx = mem.read_u32(self.ctx_slot)
            length = mem.read_u32(ctx + 0x10)
            end = mem.read_u32(ctx + 0x18)
            start = end - length
            cat_ptr = mem.read_u32(ctx + 0x1C)
            cat_raw = mem.read(cat_ptr, 64)
            nul = cat_raw.find(b"\x00")
            category = cat_raw[: nul if nul != -1 else len(cat_raw)].decode("utf-8", "replace")
            raw = mem.read(start, min(length, _READ_WINDOW))
            ja = raw[:length].decode("utf-8", "replace")
            if not ja:
                return None
            en = translate(ja, category)
            # I1 safety: `length` comes from game memory; a corrupt/huge value must never drive a huge
            # allocation or a write past the real buffer. Only handle strings that fit our read window
            # (longer ones are left untranslated rather than partially corrupted), and cap the pad to
            # that same window so the write can never exceed what we actually read.
            cap = min(length, _READ_WINDOW)
            if en and en != ja and length <= _READ_WINDOW:
                data = en.encode("utf-8", "replace") + b"\x00"
                if len(data) <= cap:  # never exceed the original buffer span
                    if len(data) < cap:  # zero-pad over the rest so no stale JA tail remains
                        data += b"\x00" * (cap - len(data))
                    mem.write(start, data)
        except Exception:  # noqa: BLE001 — never let an error skip the release below
            pass
        finally:
            # ALWAYS release the game thread, even on error — invariant I1/I4.
            mem.write(self.state_addr, struct.pack("<I", STATE_DONE))
        return ja

    def restore(self, mem: LinuxProcessMemory) -> None:
        mem.write(self.func_addr, self.saved_bytes)


def install_return_hook(
    mem: LinuxProcessMemory, func_addr: int, *, stolen_len: int,
    fields: tuple[tuple[int, int], ...] = ((0, 1 << 20),), timeout: int = SPIN_TIMEOUT,
) -> ReturnHook:
    """Install a return hook (entry + exit shellcode + shadow stack) on ``func_addr``.

    Cave layout: [state u32][ctx u32][sp u32][shadow SHADOW_BYTES][entry code][exit code]. The exit
    code's address depends on len(entry), and the entry embeds exit_code as an immediate — but the
    immediate's *value* doesn't change the entry's *length*, so we build entry once for its length,
    compute exit_code, then rebuild entry with the real exit_code and build exit.
    """
    code_reserve = 200
    total_reserve = 12 + SHADOW_BYTES + code_reserve
    cave = find_code_cave(mem, total_reserve)
    if cave is None:
        raise RuntimeError("no code cave found in rwxp regions")
    state_addr = cave + 0
    ctx_slot = cave + 4
    sp_addr = cave + 8
    shadow = cave + 12
    entry_code = cave + 12 + SHADOW_BYTES

    saved = mem.read(func_addr, stolen_len)
    # First pass: a placeholder exit_code just to measure len(entry_sc) (constant w.r.t. the imm).
    entry_sc, _ = build_return_shellcode(
        entry_code=entry_code, exit_code=0, state_addr=state_addr, ctx_slot=ctx_slot,
        sp_addr=sp_addr, shadow=shadow, func_addr=func_addr, stolen=saved, timeout=timeout,
    )
    exit_code = entry_code + len(entry_sc)
    # Second pass: real exit_code (entry length is unchanged); build the matching exit block.
    entry_sc, exit_sc = build_return_shellcode(
        entry_code=entry_code, exit_code=exit_code, state_addr=state_addr, ctx_slot=ctx_slot,
        sp_addr=sp_addr, shadow=shadow, func_addr=func_addr, stolen=saved, timeout=timeout,
    )
    if entry_code + len(entry_sc) + len(exit_sc) > cave + total_reserve:
        raise RuntimeError(
            f"return shellcode ({len(entry_sc)}+{len(exit_sc)}B) exceeds the cave reserve"
        )
    # Zero the data area (state/ctx/sp/shadow), write the code, then redirect the entry LAST.
    mem.write(state_addr, b"\x00" * (12 + SHADOW_BYTES))
    mem.write(entry_code, entry_sc)
    mem.write(exit_code, exit_sc)
    mem.write(func_addr, make_detour_jmp(func_addr, entry_code, stolen_len))
    return ReturnHook(func_addr, cave, state_addr, ctx_slot, saved, fields)


@dataclass
class BlockingHook:
    func_addr: int
    cave_addr: int
    state_addr: int
    slot_addr: int
    code_addr: int
    saved_bytes: bytes
    # (struct offset, max writable bytes) per text field reachable from the captured arg0 pointer.
    # Dialogue is a single standalone buffer at offset 0; the quest hook captures a struct with
    # several tightly-packed fields, so each field's max bytes (the gap to the next) bounds the
    # write to prevent one field's translation overflowing into the next (invariant I1).
    fields: tuple[tuple[int, int], ...] = ((0, _READ_WINDOW),)
    requests: int = field(default=0, init=False)  # game-side requests serviced (profiling/hot-hook)

    def serve_once(self, mem: LinuxProcessMemory, translate) -> str | None:
        """If a request is pending, translate each text field at the captured pointer, write back.

        Returns the first JA string handled this call (for logging), else None. Polled in a tight
        loop while installed — the game thread is blocked spinning until we set STATE=done.

        ``translate`` is normally a single ``fn(ja) -> str | None`` applied to EVERY field. For a
        surface that needs PER-FIELD routing (the quest hook's structured reward fields must use a
        reward-cleanup fn while the prose fields keep the normal translate fn), ``translate`` may
        instead be a field router exposing ``fn_for(index) -> fn``; we duck-type that (``hasattr``)
        so no import of the translate layer is needed. A plain callable is unchanged (backward
        compatible) — every existing hook keeps passing one fn for all fields.
        """
        if mem.read_u32(self.state_addr) != STATE_REQUEST:
            return None
        self.requests += 1  # a real game-side call is being serviced (hot-hook profiling)
        router = translate if hasattr(translate, "fn_for") else None
        first_ja: str | None = None
        try:
            base = mem.read_u32(self.slot_addr)
            for index, (offset, max_bytes) in enumerate(self.fields):
                try:  # one bad field must not skip the others, nor crash the loop
                    ptr = base + offset
                    raw = mem.read(ptr, min(_READ_WINDOW, max_bytes))
                    nul = raw.find(b"\x00")
                    ja_len = nul if nul != -1 else 0
                    ja = raw[:ja_len].decode("utf-8", "replace")
                    if not ja:
                        continue
                    if first_ja is None:
                        first_ja = ja
                    field_fn = router.fn_for(index) if router is not None else translate
                    en = field_fn(ja)
                    if en and en != ja:
                        self._write_back(mem, ptr, ja_len, raw, en, max_bytes)
                except Exception:  # noqa: BLE001
                    continue
        finally:
            # ALWAYS release the game thread, even on error — invariants I2/I4.
            mem.write(self.state_addr, struct.pack("<I", STATE_DONE))
        return first_ja

    @staticmethod
    def _write_back(
        mem: LinuxProcessMemory, ptr: int, ja_len: int, raw: bytes, en: str, max_bytes: int
    ) -> None:
        """Write EN into the field iff it fits (invariant I1 — never overflow / corrupt).

        Capacity = min(``max_bytes`` field slot, the JA span + the zero run that follows it). The
        zero run is the buffer's slack (self-limits at the next non-zero bytes); ``max_bytes`` caps
        it to the field's slot so a struct field can't overflow into its neighbour. If EN doesn't
        fit we skip the write (text stays Japanese) rather than corrupt memory / break the display.
        """
        cap = ja_len + 1
        while cap < len(raw) and raw[cap] == 0:
            cap += 1
        cap = min(cap, max_bytes)
        data = en.encode("utf-8", "replace") + b"\x00"
        if len(data) > cap:
            return  # doesn't fit — leave JA (display-safe)
        # Zero-pad over the rest of the original JA span so no stale bytes (e.g. a leftover <br>)
        # remain after our terminator for the game's paginator to read.
        if len(data) < ja_len + 1:
            data += b"\x00" * (ja_len + 1 - len(data))
        mem.write(ptr, data)

    def restore(self, mem: LinuxProcessMemory) -> None:
        mem.write(self.func_addr, self.saved_bytes)


def install_blocking_hook(
    mem: LinuxProcessMemory, func_addr: int, *, stolen_len: int = 6,
    fields: tuple[tuple[int, int], ...] = ((0, _READ_WINDOW),), timeout: int = SPIN_TIMEOUT,
    capture: bytes = CAPTURE_ARG0,
) -> BlockingHook:
    code_reserve = 80
    cave = find_code_cave(mem, 16 + code_reserve)
    if cave is None:
        raise RuntimeError("no code cave found in rwxp regions")
    state_addr = cave
    slot_addr = cave + 4
    code_addr = cave + 16

    saved = mem.read(func_addr, stolen_len)
    shellcode = build_blocking_shellcode(
        code_addr=code_addr, state_addr=state_addr, slot_addr=slot_addr,
        func_addr=func_addr, stolen=saved, timeout=timeout, capture=capture,
    )
    # The cave reserves `code_reserve` bytes for code; refuse to write past it rather than
    # silently clobber adjacent cave memory (a longer `capture`/`stolen` could overflow it).
    if len(shellcode) > code_reserve:
        raise RuntimeError(f"shellcode {len(shellcode)}B exceeds {code_reserve}B code reserve")
    mem.write(state_addr, b"\x00" * 16)
    mem.write(code_addr, shellcode)
    mem.write(func_addr, make_detour_jmp(func_addr, code_addr, stolen_len))
    return BlockingHook(func_addr, cave, state_addr, slot_addr, code_addr, saved, fields)


@dataclass
class PlayerHook:
    """READ-ONLY blocking hook for the player-login struct (auto-detect player + sibling names).

    Reuses the SAME blocking detour machinery as ``BlockingHook`` (same cave, same shellcode, same
    STATE/SLOT handshake — see ``install_player_hook``), so there is no duplicated detour/shellcode
    logic; only the serve action differs. Where ``BlockingHook.serve_once`` translates a text field
    and writes it BACK into the game, ``PlayerHook.serve_once`` only READS three strings out of the
    captured struct and feeds them to ``apply_names`` — it never writes anywhere in the game except
    the STATE-release dword (invariant: a bug here can't corrupt/crash the game's memory).
    """

    func_addr: int
    cave_addr: int
    state_addr: int
    slot_addr: int
    code_addr: int
    saved_bytes: bytes
    requests: int = field(default=0, init=False)  # game-side login requests serviced (profiling)

    @staticmethod
    def _read_cstring(mem: LinuxProcessMemory, addr: int, cap: int = 64) -> str:
        """Read a NUL-terminated UTF-8 string at ``addr``, capped at ``cap`` bytes (stop at NUL)."""
        raw = mem.read(addr, cap)
        nul = raw.find(b"\x00")
        # No NUL within the cap -> treat as garbage, not a name. A real name is NUL-terminated well
        # within 64 bytes; returning the raw cap bytes would store an unterminated garbage sibling.
        if nul == -1:
            return ""
        return raw[:nul].decode("utf-8", "replace")

    def serve_once(self, mem: LinuxProcessMemory, apply_names) -> str | None:
        """If a login request is pending, READ the player/sibling names + relationship and apply them.

        Reads the player name (NUL-terminated UTF-8) at struct+24, the sibling name at struct+100,
        and the 1-byte relationship at struct+119, then calls ``apply_names(player_ja, sibling_ja,
        relationship)`` when the player name is non-empty. ALWAYS releases the blocked login thread
        (STATE -> DONE) in a finally — even on error — so a once-per-login read can never hang the
        game. The ONLY write this method ever makes is that STATE-release dword: it is strictly
        read-only with respect to the game's own memory (no write-back into the struct).

        Returns the Japanese player name ONLY when ``apply_names`` actually applied a CHANGE (so the
        caller logs/notifies exactly once per real change); returns None when nothing was pending,
        the name was empty, a read raised, or the names were unchanged (idempotent).
        """
        from . import signatures as sig

        # Early no-op BEFORE entering the active-request path: there is no pending login, so we read
        # nothing and (critically) write NOTHING — the STATE-release below must not run on this path.
        if mem.read_u32(self.state_addr) != STATE_REQUEST:
            return None
        self.requests += 1  # a real game-side login call is being serviced (hot-hook profiling)

        # From here on a request IS pending: we are committed to releasing STATE in the finally, no
        # matter what happens (read error, apply error). `applied` flips True ONLY after a successful
        # apply_names(...) that itself reported a real change, so an error after reading the player
        # name (e.g. the sibling read raising) can never make us falsely report success.
        player_ja: str = ""
        applied = False
        try:
            base = mem.read_u32(self.slot_addr)
            player_ja = self._read_cstring(mem, base + sig.PLAYER_NAME_OFFSET)
            if player_ja:
                sibling_ja = self._read_cstring(mem, base + sig.PLAYER_SIBLING_OFFSET)
                rel_raw = mem.read(base + sig.PLAYER_RELATIONSHIP_OFFSET, 1)
                relationship = rel_raw[0] if rel_raw else 0
                if apply_names(player_ja, sibling_ja, relationship):
                    applied = True  # real change applied -> caller should log this login
        except Exception:  # noqa: BLE001 — never let an error skip the STATE release below
            applied = False  # a read/apply error is NOT a success, even if player_ja was read
        finally:
            # ALWAYS release the blocked login thread on the active-request path, even on error.
            # This is the ONLY write this method ever makes into the game's memory.
            mem.write(self.state_addr, struct.pack("<I", STATE_DONE))
        return player_ja if applied else None

    def restore(self, mem: LinuxProcessMemory) -> None:
        mem.write(self.func_addr, self.saved_bytes)


def install_player_hook(
    mem: LinuxProcessMemory, func_addr: int, *, stolen_len: int = 6, timeout: int = SPIN_TIMEOUT,
    capture: bytes = CAPTURE_ARG0,
) -> PlayerHook:
    """Install the READ-ONLY player-login detour, reusing ``install_blocking_hook``'s cave + detour.

    The login struct must be guaranteed valid while we read three strings out of it, so this BLOCKS
    (a brief, once-per-login spin) exactly like the text hooks — an async/ring capture could read a
    freed struct. We therefore install the identical blocking detour and simply adopt its cave
    addresses into a ``PlayerHook`` whose serve action is read-only. No detour/shellcode code is
    duplicated.
    """
    b = install_blocking_hook(
        mem, func_addr, stolen_len=stolen_len, timeout=timeout, capture=capture
    )
    return PlayerHook(
        func_addr=b.func_addr, cave_addr=b.cave_addr, state_addr=b.state_addr,
        slot_addr=b.slot_addr, code_addr=b.code_addr, saved_bytes=b.saved_bytes,
    )


@dataclass
class InstalledHook:
    func_addr: int
    cave_addr: int
    head_addr: int
    ring_addr: int
    code_addr: int
    saved_bytes: bytes
    _tail: int = 0

    def poll(self, mem: LinuxProcessMemory) -> list[int]:
        """Return new text pointers recorded since the last poll."""
        head = mem.read_u32(self.head_addr)
        out: list[int] = []
        while self._tail != head:
            ptr = mem.read_u32(self.ring_addr + self._tail)
            if ptr:
                out.append(ptr)
            self._tail = (self._tail + 4) & RING_MASK
        return out

    def restore(self, mem: LinuxProcessMemory) -> None:
        mem.write(self.func_addr, self.saved_bytes)


def install_capture_hook(
    mem: LinuxProcessMemory, func_addr: int, *, stolen_len: int = 6
) -> InstalledHook:
    """Install the capture detour on ``func_addr``. Raises RuntimeError if no cave is found."""
    # cave: [head u32][ring RING_BYTES][code]
    code_reserve = 64
    cave = find_code_cave(mem, 4 + RING_BYTES + code_reserve)
    if cave is None:
        raise RuntimeError("no code cave found in rwxp regions")
    head_addr = cave
    ring_addr = cave + 4
    code_addr = cave + 4 + RING_BYTES

    saved = mem.read(func_addr, stolen_len)
    shellcode = build_capture_shellcode(
        code_addr=code_addr, head_addr=head_addr, ring_addr=ring_addr,
        func_addr=func_addr, stolen=saved,
    )
    # Zero the data area, write shellcode, then redirect the function entry last.
    mem.write(head_addr, b"\x00" * (4 + RING_BYTES))
    mem.write(code_addr, shellcode)
    mem.write(func_addr, make_detour_jmp(func_addr, code_addr, stolen_len))
    return InstalledHook(func_addr, cave, head_addr, ring_addr, code_addr, saved)
