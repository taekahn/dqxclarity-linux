"""Verbose, isolated diagnostic for the walkthrough ("Story So Far") hook.

Installs ONLY the walkthrough hook, then on each blocking request logs the captured address, the JA,
the EN, the capacity math, and a read-back of the buffer after writing. This pinpoints whether the
problem is (a) translation, (b) capacity/fit, or (c) the game not displaying from the buffer we wrote.
"""

from __future__ import annotations

import struct
import time

from dqxclarity import config as cfg_mod
import dqxclarity.cli as cli
from dqxclarity.process import hooks as hookmod
from dqxclarity.process.detour import STATE_DONE, STATE_REQUEST
from dqxclarity.process.discover import find_game_pid
from dqxclarity.process.memory_linux import LinuxProcessMemory
from dqxclarity.runtime.dispatch import build_translate_fn

pid = find_game_pid()
mem = LinuxProcessMemory(pid)
cfg = cfg_mod.load()
tr = cli._build_translator(cfg)
tr.start()

spec = hookmod.HOOKS["walkthrough"]
addr = hookmod.find_function(mem, spec)
print(f"walkthrough func @ {hex(addr) if addr else None}", flush=True)
hook = hookmod.install(mem, hookmod.FoundHook(spec, addr))
fn, _ = build_translate_fn(cfg, tr, wrap_width=31, lines_per_page=0, sync=True)
print("installed — open the Story So Far panel now (40s window)...", flush=True)

deadline = time.time() + 75
n = 0
last_beat = 0.0
try:
    while time.time() < deadline:
        now = time.time()
        if now - last_beat >= 10:
            print(f"    ...listening ({int(deadline - now)}s left)", flush=True)
            last_beat = now
        if mem.read_u32(hook.state_addr) == STATE_REQUEST:
            base = mem.read_u32(hook.slot_addr)
            raw = mem.read(base, 512)
            nul = raw.find(b"\x00")
            ja = raw[:nul].decode("utf-8", "replace") if nul > 0 else ""
            n += 1
            print(f"\n[{n}] addr={hex(base)} ja_len={nul} ja={ja!r}", flush=True)
            if ja:
                en = fn(ja)
                print(f"    en={en!r}", flush=True)
                if en and en != ja:
                    data = en.encode("utf-8", "replace") + b"\x00"
                    cap = nul + 1
                    while cap < len(raw) and raw[cap] == 0:
                        cap += 1
                    print(f"    cap={cap} en_bytes={len(data)} fits={len(data) <= cap}", flush=True)
                    if len(data) <= cap:
                        if len(data) < nul + 1:
                            data += b"\x00" * (nul + 1 - len(data))
                        mem.write(base, data)
                        back = mem.read(base, min(80, cap))
                        print(f"    WROTE; readback={back!r}", flush=True)
            mem.write(hook.state_addr, struct.pack("<I", STATE_DONE))
        else:
            time.sleep(0.001)
finally:
    hook.restore(mem)
    tr.stop()
    print(f"\ndone. {n} walkthrough request(s) handled. hook restored.", flush=True)
