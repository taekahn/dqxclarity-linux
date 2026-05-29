"""Phase 3 spike: can Frida attach to DQXGame.exe running under Proton WOW64?

Read-only: attaches, reports arch/pointer size, enumerates ELF modules and file-backed memory
ranges (PE modules from Wine usually show up as ranges, not ELF modules), resolves the
DQXGame.exe mapping, and reads its first bytes to confirm 'MZ'. No hooks installed.

Decides the Phase 3 hooking backend:
  * attach + resolve DQXGame.exe + read works  -> Frida backend viable (reuse upstream hooks)
  * attach fails / can't see the PE            -> native ptrace detour backend
"""

from __future__ import annotations

import sys
import time

import frida

from dqxclarity.process.discover import find_game_pid

JS = r"""
const out = {
  arch: Process.arch,
  platform: Process.platform,
  pointerSize: Process.pointerSize,
  pageSize: Process.pageSize,
};
// 1) ELF modules frida knows about
const mods = Process.enumerateModules();
out.moduleCount = mods.length;
out.sampleModules = mods.slice(0, 6).map(m => m.name);
let dqxMod = mods.find(m => m.name.toLowerCase().indexOf("dqxgame") !== -1);

// 2) file-backed ranges (PE images mapped by Wine usually appear here)
let dqxRange = null;
const ranges = Process.enumerateRanges("r--");
for (const r of ranges) {
  if (r.file && r.file.path && r.file.path.toLowerCase().indexOf("dqxgame.exe") !== -1) {
    if (!dqxRange || r.base.compare(dqxRange.base) < 0) dqxRange = r;
  }
}
out.dqxAsModule = dqxMod ? { name: dqxMod.name, base: dqxMod.base.toString(), size: dqxMod.size } : null;
out.dqxAsRange = dqxRange ? { base: dqxRange.base.toString(), path: dqxRange.file.path } : null;

// 3) read the PE header at whichever base we found
const base = dqxRange ? dqxRange.base : (dqxMod ? dqxMod.base : null);
let header = null;
if (base) {
  try { header = base.readByteArray(2); } catch (e) { out.readError = e.message; }
}
send(out, header);
"""


def main() -> int:
    pid = find_game_pid()
    if pid is None:
        print("game not running")
        return 2
    print(f"target pid: {pid}")

    try:
        session = frida.attach(pid)
    except Exception as e:  # noqa: BLE001 - we want the exact failure mode
        print(f"ATTACH FAILED ({type(e).__name__}): {e}")
        print(">> Frida backend NOT viable on this setup -> use native detours.")
        return 1
    print("attach: OK")

    messages: list = []
    script = session.create_script(JS)
    script.on("message", lambda msg, data: messages.append((msg, data)))
    try:
        script.load()
    except Exception as e:  # noqa: BLE001
        print(f"SCRIPT LOAD FAILED ({type(e).__name__}): {e}")
        session.detach()
        return 1

    time.sleep(1.5)  # let send() messages arrive
    for msg, data in messages:
        if msg.get("type") == "error":
            print("JS ERROR:", msg.get("description"))
            continue
        payload = msg.get("payload", {})
        for k, v in payload.items():
            print(f"  {k}: {v}")
        if data:
            print(f"  pe_header_bytes: {data!r}  -> {'MZ OK' if data[:2] == b'MZ' else 'NOT MZ'}")

    session.detach()
    print(">> Frida backend VIABLE." if any(
        m[0].get("payload", {}).get("dqxAsRange") or m[0].get("payload", {}).get("dqxAsModule")
        for m in messages if m[0].get("type") != "error"
    ) else ">> Attached, but could not resolve DQXGame.exe -> lean native detours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
