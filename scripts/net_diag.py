"""Focused network_text diagnostic: logs JA content + category + EN + write result, continuously.

Confirms the return hook reads the produced string and its category correctly (esp. the Story So Far
recap, category <%sM_kaisetubun>) and that we translate + write it. Runs until Ctrl-C — no window.
"""

from __future__ import annotations

import struct
import time

from dqxclarity import config as cfg_mod
import dqxclarity.cli as cli
from dqxclarity.process import hooks as hookmod
from dqxclarity.process.detour import STATE_DONE, STATE_REQUEST, _READ_WINDOW
from dqxclarity.process.discover import find_game_pid
from dqxclarity.process.memory_linux import LinuxProcessMemory
from dqxclarity.runtime.dispatch import build_network_translate_fn, is_japanese

mem = LinuxProcessMemory(find_game_pid())
cfg = cfg_mod.load()
tr = cli._build_translator(cfg)
tr.start()

spec = hookmod.HOOKS["network_text"]
addr = hookmod.find_function(mem, spec)
hook = hookmod.install(mem, hookmod.FoundHook(spec, addr))
fn = build_network_translate_fn(cfg, tr)
print(f"network_text @ {hex(addr)} — open the Story So Far panel; Ctrl-C to stop.\n", flush=True)

seen_cats: dict[str, int] = {}
try:
    while True:
        if mem.read_u32(hook.state_addr) != STATE_REQUEST:
            time.sleep(0.001)
            continue
        try:
            ctx = mem.read_u32(hook.ctx_slot)
            length = mem.read_u32(ctx + 0x10)
            end = mem.read_u32(ctx + 0x18)
            start = end - length
            cat_ptr = mem.read_u32(ctx + 0x1C)
            cat_raw = mem.read(cat_ptr, 64)
            category = cat_raw[: cat_raw.find(b"\x00")].decode("utf-8", "replace")
            raw = mem.read(start, min(length, _READ_WINDOW))
            ja = raw[:length].decode("utf-8", "replace")
            seen_cats[category] = seen_cats.get(category, 0) + 1
            # only log Japanese-bearing strings in detail (skip the ambient template tokens)
            if is_japanese(ja):
                en = fn(ja, category)
                print(f"[{category}] len={length}\n    JA={ja!r}\n    EN={en!r}", flush=True)
                if en and en != ja and length <= _READ_WINDOW:
                    data = en.encode("utf-8", "replace") + b"\x00"
                    if len(data) <= length:
                        data += b"\x00" * (length - len(data))
                        mem.write(start, data)
                        print("    wrote EN", flush=True)
        finally:
            mem.write(hook.state_addr, struct.pack("<I", STATE_DONE))
except KeyboardInterrupt:
    pass
finally:
    hook.restore(mem)
    tr.stop()
    print("\nstopped; hook restored.\ncategories seen:", flush=True)
    for c, n in sorted(seen_cats.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4}  {c}", flush=True)
