"""Full-detail, no-timing-window capture across all installed hooks.

Mirrors the real serve loop (translates + writes back, so the game stays translated) but logs the
COMPLETE captured Japanese, the English, and the write result for every field on every surface.
Runs until killed (Ctrl-C / SIGINT) — no countdown to race. Use it to see exactly which surface
carries a given on-screen string (e.g. the "Story So Far" recap body) and why it does/doesn't
translate.

    .venv/bin/python scripts/capture_all.py [hook1,hook2,...]   (default: dialogue,quest,walkthrough)
"""

from __future__ import annotations

import struct
import sys
import threading
import time

from dqxclarity import config as cfg_mod
import dqxclarity.cli as cli
from dqxclarity.process import hooks as hookmod
from dqxclarity.process.detour import STATE_DONE, STATE_REQUEST, _READ_WINDOW
from dqxclarity.process.discover import find_game_pid
from dqxclarity.process.memory_linux import LinuxProcessMemory
from dqxclarity.runtime.dispatch import build_translate_fn

names = (sys.argv[1] if len(sys.argv) > 1 else "dialogue,quest,walkthrough").split(",")

pid = find_game_pid()
if pid is None:
    print("game not running", flush=True)
    sys.exit(1)
mem = LinuxProcessMemory(pid)
cfg = cfg_mod.load()
tr = cli._build_translator(cfg)
tr.start()

installed = []  # (name, BlockingHook, translate_fn)
for fh in hookmod.locate(mem, names):
    hook = hookmod.install(mem, fh)
    fn, _ = build_translate_fn(
        cfg, tr, wrap_width=fh.spec.wrap_width,
        lines_per_page=fh.spec.lines_per_page, sync=fh.spec.sync,
    )
    installed.append((fh.spec.name, hook, fn))
    print(f"hooked {fh.spec.name} @ {hex(fh.func_addr)}", flush=True)
print(f"capturing on pid {pid} (Ctrl-C to stop). Open the panel whenever you're ready.\n", flush=True)

stop = threading.Event()
try:
    while not stop.is_set():
        idle = True
        for name, hook, fn in installed:
            if mem.read_u32(hook.state_addr) != STATE_REQUEST:
                continue
            idle = False
            base = mem.read_u32(hook.slot_addr)
            for offset, max_bytes in hook.fields:
                ptr = base + offset
                raw = mem.read(ptr, min(_READ_WINDOW, max_bytes))
                nul = raw.find(b"\x00")
                ja_len = nul if nul != -1 else 0
                ja = raw[:ja_len].decode("utf-8", "replace")
                if not ja:
                    continue
                en = fn(ja)
                wrote = False
                if en and en != ja:
                    hook._write_back(mem, ptr, ja_len, raw, en, max_bytes)
                    wrote = (mem.read(ptr, 4) != raw[:4])
                print(f"[{name}] off={offset} ja_len={ja_len}\n    JA={ja!r}\n    EN={en!r}  wrote={wrote}",
                      flush=True)
            mem.write(hook.state_addr, struct.pack("<I", STATE_DONE))
        if idle:
            time.sleep(0.001)
except KeyboardInterrupt:
    pass
finally:
    for _name, hook, _fn in installed:
        hook.restore(mem)
    tr.stop()
    print("\nstopped; hooks restored.", flush=True)
