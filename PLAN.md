# dqxclarity-linux — Design & Implementation Plan

A Linux-native, CLI-only fork of [dqxclarity](https://github.com/dqx-translation-project/dqxclarity)
that translates **Dragon Quest X Online** running under Steam + Proton/Wine. It provides
both **static file patching** (curated/hand translations) and **live in-memory translation**
(machine-translated dynamic text).

---

## 1. How upstream dqxclarity works (the thing we're porting)

dqxclarity is ~51% Python, ~39% C#, ~5% TypeScript. The pieces:

| Layer | Tech (Windows) | Role |
|---|---|---|
| Launcher / GUI | C# (.NET 9) | Bypasses the official launcher, saves creds, drives the Python core. |
| Core | Python 3.11 **32-bit** | Orchestration, translation pipeline, DB, file patching. |
| Memory I/O | `pymem` (`OpenProcess`/`ReadProcessMemory`) | Read/write the game's process memory. |
| Hooking | **Frida** + TypeScript agents | Intercept the functions that render dialog/quest/NPC text. |
| Translation cache | SQLite (`clarity_dialog.db`) | Caches JA→EN; ships curated/hand translations. |
| MT providers | `app/common/translators/*` | DeepL, Google (official/free/PA), OpenAI/ChatGPT, LibreTranslate, Ollama, Yandex. |
| File patching | `lib.py` + `dqx_translations` repo | Drops pre-modded `.dat0`/`.idx` archives + patched `.exe`s into the game dir. |

**Two translation tiers** (both must be ported):
1. **Static** — hand-translated UI/menus/items/skills/story shipped as modded DAT/IDX archives and as
   rows in the bundled SQLite DB. Deterministic; no API needed.
2. **Live/dynamic** — text not covered by the static set (player-generated names, untranslated
   dialog). A Frida hook catches the JA string at render time, looks it up in the DB; on a miss it
   queues an MT call, stores the result, and writes EN back into the game's memory.

DQX shares Square Enix's FFXIV-style packed archive format (`.idx` index + `.dat0` data, SqPack-like).
Upstream does **not** repack archives at runtime — it downloads **pre-built** modded DATs from the
community translation project and copies them into the game's data folder. So "file patching" is,
mechanically, *download + copy files to the right paths* — fully OS-portable.

---

## 2. The key Linux insight

**Wine/Proton is not an emulator.** DQXGame.exe's x86 machine code executes natively on the CPU;
Wine only translates Windows API/syscalls. Therefore the game is just a normal Linux process whose
address space contains the PE mapped in. That gives us everything we need *without Windows*:

- **Read/write memory** via `process_vm_readv(2)` / `process_vm_writev(2)` (and `/proc/<pid>/mem`).
  This is the Linux equivalent of `ReadProcessMemory`/`WriteProcessMemory`, and — unlike pymem — it
  works fine from a normal **64-bit** Python process even when the target is a **32-bit** game
  (we just treat pointers as 32-bit when parsing structures). No 32-bit interpreter needed.
- **Find module bases & ranges** by parsing `/proc/<pid>/maps` (the mapping for `DQXGame.exe` shows
  the file path; its sections give us base + size for AOB scanning).
- **Code patching / detours** — because the code runs natively, x86 trampolines/shellcode we inject
  into a code cave execute normally. Classic detour hooking works regardless of Wine.

The only Linux gotchas:
- `ptrace`/`process_vm_*` need `PTRACE_MODE_ATTACH` permission. With matching UID this is usually fine,
  but `kernel.yama.ptrace_scope=1` restricts attaching to non-children. Mitigation: launch the game as
  our child, or document `sysctl kernel.yama.ptrace_scope=0` / a `CAP_SYS_PTRACE` setcap.
- Game files live in the **Steam install dir** (actual `Game/` data) plus a **Proton prefix**
  (`steamapps/compatdata/<appid>/pfx`). The CLI must resolve both.

---

## 3. Tech stack decision

**Recommendation: Python 3.11+ core (clean-room-ish fork), with a native Linux memory backend.**

Rationale:
- Maximizes reuse of upstream's portable logic: the translator providers, SQLite schema/`db_ops`,
  signature definitions, and translation pipeline are pure Python and largely OS-agnostic.
- Lets us track upstream changes (signatures shift when the game patches).
- The only genuinely new code is a `LinuxProcessMemory` backend mirroring pymem's surface so the rest
  of the code is reusable, plus the hooking layer.

Specifics:
- **Memory backend**: `ctypes` wrappers around `process_vm_readv`/`writev` + `/proc/<pid>/maps`
  parsing. (Optionally a small Rust/PyO3 module later if scan speed matters; not needed for MVP.)
- **CLI**: `typer` (or `click`). Config in TOML mirroring upstream's `user_settings.ini`.
- **Packaging**: `uv` project; `pipx`-installable; single `dqxclarity` entrypoint.
- **Drop**: C# launcher (Windows GUI) and PowerShell — not needed for a CLI tool. The Proton launch
  is handled by `steam`/`proton` directly or a thin wrapper script.

*Alternative considered:* full Rust rewrite (single static binary, great for the memory layer) — but
it discards reusable Python and slows upstream tracking. Reserve Rust for an optional perf hot-path.

---

## 4. Target architecture

```
dqxclarity-linux/
  cli.py                 # typer entrypoint: patch | run | translate | doctor
  config.py              # TOML config; resolves Steam/Proton paths
  process/
    discover.py          # find DQXGame.exe pid under Proton; resolve install + prefix
    memory_linux.py      # process_vm_readv/writev + /proc/<pid>/maps  (pymem-shaped API)
    signatures.py        # AOB patterns (ported from upstream), scanner
  hooking/
    backend_frida.py     # Frida session/agent injection (primary attempt)
    backend_native.py    # ptrace + code-cave detour injection (fallback)
    agents/*.ts|*.js     # text-intercept scripts (reused from upstream if Frida works)
    dispatch.py          # JA string in -> lookup/queue/translate -> EN string out
  translate/
    db.py                # SQLite clarity_dialog.db (cache + curated rows)
    pipeline.py          # lookup -> MT -> store; name romanization
    providers/           # deepl, google(*), openai, libretranslate, ollama, yandex
  patching/
    files.py             # download + place modded .dat0/.idx; patch config/launcher exe
    sources.py           # pull from dqx_translations release artifacts
  doctor.py              # preflight: ptrace_scope, paths, deps, process found
```

---

## 5. Component-by-component porting notes

### 5a. Process discovery & memory backend  *(new Linux code — moderate)*
- `discover.py`: scan `/proc/*/cmdline` (or `pgrep -f DQXGame.exe`) for the Wine-hosted game; read
  `/proc/<pid>/maps` to get the `DQXGame.exe` base address and section ranges. Auto-detect the Steam
  library, the non-Steam-game `appid`, and `compatdata/<appid>/pfx`.
- `memory_linux.py`: `read_bytes(addr,n)`, `write_bytes(addr,b)`, `read_<type>`, `alloc` (via the
  native hook backend / `mmap` in target through a stub), pattern scan. Mirror pymem method names so
  ported upstream code calls the same API.
- 32-bit pointer handling: parse pointers as 4 bytes; keep an `is64` flag for future-proofing.

### 5b. Hooking layer  *(DECIDED: native detours — Frida eval failed)*
The pivotal decision, now resolved by the Phase 3 spike (`spikes/frida_wow64_eval.py`, run
2026-05-29):

- **Backend A — Frida — ❌ REJECTED (eval failed).** `frida.attach(pid)` fails on this Proton
  **WOW64** setup with `NotSupportedError: unable to perform ptrace pokedata: Input/output error`.
  Frida injects its agent via `ptrace(PTRACE_POKEDATA)`, which is incompatible with the Wine WOW64
  process — a *different, stricter* mechanism than the `process_vm_readv/writev` we use (and which
  works). It's `EIO`, not `EPERM`, so it isn't a `ptrace_scope` tweak; it's fundamental. (Reusing
  upstream's TS hooks would have saved effort, but the door is closed on WOW64. A non-WOW64 Proton
  build *might* let a 32-bit Frida attach, but that's fragile and not worth forcing.)
- **Backend B — Native detours — ✅ CHOSEN.** AOB-scan for the text-render functions, write an x86
  trampoline into a code cave (the game image already has `rwxp` regions, so no `mprotect` needed),
  redirect to injected shellcode that hands the JA pointer out to Python (shared ring buffer / pipe).
  This is the pre-Frida dqxclarity technique and is **immune to Wine quirks** because it only
  manipulates native code the CPU already runs. **Cost:** reimplement each hook + maintain shellcode.
  Already de-risked: live R/W proven (Phase 0), and the scanner to find functions exists (Phase 2).

`dispatch.py` is backend-agnostic: given an intercepted JA string + write-back address, it does
DB lookup → queue/translate → write EN (respecting the original buffer's length; longer strings need
relocation into an allocated buffer + pointer fixup, same constraint upstream handles).

### 5c. Translation pipeline  *(mostly portable — easy)*
- Port `db.py`/`db_ops` and the `clarity_dialog.db` schema as-is; ship the curated DB.
- Port providers under `providers/`; they're HTTP/stdlib. Free Google/LibreTranslate/Ollama work
  with zero credentials → good default so the tool is useful without paid API keys.
- Name/romaji handling for player & sibling names ported from upstream.

### 5d. File patching  *(fully portable — easy, good first deliverable)*
- `sources.py`: fetch the latest modded `.dat0`/`.idx` (and optional patched config/launcher `.exe`)
  from the `dqx_translations` release artifacts.
- `files.py`: back up originals, copy modded files into the resolved `Game/Content/Data` path inside
  the Steam install; integrity-check.
- **Important safety port:** refuse to patch while the official patcher is mid-update (upstream warns
  this can corrupt the install). Detect and guard.

### 5e. CLI & config  *(new — easy)*
```
dqxclarity doctor            # preflight checks (ptrace_scope, paths, deps, game running)
dqxclarity patch [--config] [--launcher]   # static file patching
dqxclarity run               # attach + live translation loop (foreground, Ctrl-C to stop)
dqxclarity config            # show/edit resolved paths & provider keys
```
TOML config: game install dir, Proton prefix, MT provider + keys, toggles per hook.

---

## 6. Phased roadmap (de-risk first, value early)

- **Phase 0 — memory/hooking spike — ✅ DONE (run against the live game, 2026-05-29).**
  Results: `process_vm_readv` **and** `process_vm_writev` read/write the live game with **zero
  elevation** despite `ptrace_scope=1` (`/proc/<pid>/mem` works too). Game is **32-bit** (PE base
  `0x00400000`) under Proton **WOW64** (`PROTON_USE_WOW64=1`). `rwxp` code-cave regions exist in the
  image → **native detour hooking is viable**, so Frida is off the critical path (untested; optional
  eval via `pip install '.[frida]'`). Spike is productized as `dqxclarity probe` +
  `process/memory_linux.py`.

- **Phase 1 — Static file patching — ✅ DONE (MVP, verified against live game + unit-tested).**
  Implemented: live-process + static install discovery with auto-persist/refresh of `install_root`
  (`process/discover.py`); TOML config (`config.py`); a manifest-driven, **idempotent**
  download→(verify)→backup→atomic-replace engine with a game-running safety block, added-file
  tracking, and `restore` (`patching/`); a `typer` CLI (`doctor`, `probe`,
  `patch [--config|--launcher|--dry-run|--force]`, `restore`, `config show|set`); pytest coverage of
  the engine. The bundled manifest mirrors upstream's real `PatchService`: three GitHub
  "latest-release" groups — **game_files** (`data00000000.win32.dat1` + `.idx` from the `dqxclarity`
  repo; the main static UI/menu translation), **config_exe** (`DQXConfig.exe` from `dqx_en_config`),
  **launcher_exe** (`DQXLauncher.exe` from `dqx_en_launcher`). No admin needed on Linux (install is
  user-writable). All asset URLs verified to resolve. **Remaining:** run a real `patch` against a
  closed game to confirm in-game (pending user go-ahead on a real install).

- **Phase 2 — Memory backend + signatures — ✅ DONE (live-validated).**
  `memory_linux.pattern_scan` + ported `signatures.py` + `dqxclarity scan`; confirmed it locates
  and reads real JA strings in the running game.

- **Phase 3a — Live name translation (scanner-based, no hooks) — ✅ BUILT (unit-tested + components
  live-validated).** No-MT-required base per the provider discussion (§10): curated-DB cache
  (in-memory hot dict + SQLite-WAL, §9) + local `pykakasi` romanization for player/NPC names, plus an
  optional **`claude_cli`** MT provider (batched headless `claude -p`, runs on the user's
  subscription, default off). `runtime/names_loop.py` polls the name patterns → translate → safe
  bounded write-back; CLI `romanize`, `translate-text`, `names`. 11 tests pass; romanizer and
  `claude_cli` validated live (`["こんにちは","せかい"]`→`["Hello","World"]`). **Remaining:** confirm
  in-game name write-back with party/NPCs on screen (the volatile name patterns hit 0 when that
  content isn't visible and may need re-verification against this game build).

- **Phase 3b — Dialogue via native detours — 🚧 IN PROGRESS (machinery built + unit-tested;
  capture proven live; write-back next).** `process/detour.py`: code-cave finder (zero-run in the
  game image's own rwxp sections — *not* anonymous low Wine pages), x86 trampoline assembler
  (`jmp`+NOP over the 6-byte dialogue prologue), capture shellcode (pushad/pushfd → record arg0
  text ptr to a ring buffer → stolen bytes → jmp back), and install/poll/restore. Dialogue sig +
  `__thiscall` arg layout from upstream `scripts/dialogue.ts` (this=ecx, **arg0 = text ptr**).
  **LIVE-VALIDATED:** injected at `0xb4e730` (no crash), captured real multi-line NPC dialogue
  matching the on-screen text (user-confirmed); arg0 reads the full conversation string (`\n`
  lines, `<br>` page breaks). Async stale reads (freed buffers) are filtered by JA validity.
  **KEY FINDING (user-observed):** the function fires **once per line**, *not* repeatedly while a
  line is shown → **async write-back is insufficient; must BLOCK** like upstream. **Next:** blocking
  shellcode — capture ptr + set request flag + spin-wait on a done flag (with timeout) while Python
  reads JA → translates (Phase 3a pipeline) → writes EN into the buffer → sets done. Handle EN-longer
  -than-JA (probe buffer capacity; EN of a JA sentence is often longer in bytes).

- **Phase 4 — Polish.**
  `doctor` UX, install docs, ptrace permission guidance, optional Proton-launch wrapper, packaging
  (`pipx`), upstream-sync notes for when signatures break after a game patch.

---

## 7. Risks & open questions

- **Frida ↔ Wine compatibility (biggest unknown)** — resolved by the Phase 0 spike. The native
  fallback exists precisely so the project isn't blocked on it.
- **ptrace permissions** — needs documented sysctl/setcap or child-launch model.
- **Game-patch fragility** — signatures and DAT offsets break when SE patches DQX; we inherit
  upstream's maintenance burden. Tracking upstream as a fork mitigates this.
- **Write-back length constraints** — replacing JA with longer EN needs buffer relocation + pointer
  fixups; port upstream's approach carefully.
- **Legal/ToS** — same posture as upstream (client-side translation, no server interaction); we add
  no new risk but should keep the disclaimer.
- **Path resolution for non-Steam game** — the appid is synthetic; need robust detection of the
  install dir + `compatdata` prefix.

## 8. Decisions (resolved 2026-05-29)
1. **Soft fork** — clean-room Linux scaffolding, reuse upstream's portable Python (providers, DB,
   signatures) to track updates. ✅
2. **Hooking backend** — decided by the Phase 0 spike (Frida vs native detours). ✅
3. **MVP = Phase 1 (static file patching)** shipped first as a standalone useful release, then live
   translation. ✅

---

## 9. Data layer assessment (live-translation cache)

**Conclusion: keep SQLite, but demote it off the hot path behind an in-memory cache.** Once the hot
path and the durable store are separated, the engine choice stops being a performance question.

### Why the DB is not the live bottleneck
The synchronous hot-path question on the game thread is a single point lookup: "do I have EN for this
JA key *now*?" — and it must not block. Order-of-magnitude latencies:

| Operation | Latency | |
|---|---|---|
| Python `dict` lookup | ~50–100 ns | in-heap, no I/O, no lock |
| LMDB `get` | ~1 µs | mmap, zero-copy, lock-free reads |
| SQLite indexed point lookup (warm, in-proc) | ~10–30 µs | + GIL/marshalling |
| Redis `GET` (localhost) | ~30–100 µs | network hop + serialize, separate process |
| Frida agent ↔ Python RPC round-trip | ~0.1–1 ms | the real per-hit tax |
| MT API call | ~100–1000 ms | the actual bottleneck |

The DB is 3–4 orders faster than the API and 10–100× faster than the Frida IPC. The bottleneck is the
API (mitigated by caching) then the IPC, never the DB engine.

### Architecture: cache-aside + write-behind
1. **Hot tier — in-memory `dict`/LRU.** Curated set is static and small (tens of MB, ≤ a few hundred k
   entries) → load into RAM at startup; warm with dynamic hits. Game thread touches only this.
2. **Durable tier — SQLite (WAL), background thread.** Stores dynamically MT'd strings (batched
   write-behind) and rehydrates the in-memory cache at startup. WAL → readers never block the writer.

SQLite's only job becomes durability + cold-start hydration, for which it's ideal here: embedded,
zero-ops, single file, stdlib, transactional.

### Alternatives
- **In-memory dict/LRU** — must-have hot path. Not optional.
- **SQLite (WAL)** — keep as persistence/hydration layer.
- **LMDB** — only alternative worth benchmarking; mmap zero-copy ~1µs reads, single-writer (we have
  one). Consider only if Python-heap pressure or cold-start time becomes real.
- **Redis/Memcached** — reject: separate server + round-trip slower than embedded, ops burden, no win.
- **RocksDB/LevelDB** — reject: write-optimized LSM, we're read-heavy, native deps + write-amp.
- **DuckDB** — reject: columnar/analytical, wrong shape for point lookups.
- **Flat JSON/msgpack → dict** — viable for shipping the *static* set; SQLite still wins write-side.

### Bigger levers than the engine
- **Push the hot cache into the Frida agent (JS `Map`)** so cache hits resolve in-process and skip the
  ~0.1–1ms Python round-trip — a bigger win than any DB swap. Sync a hot subset; fall to Python on
  miss. Phase-3 spike.
- **Matching logic, not storage:** normalized keys / placeholder (player-name) handling want a
  normalized-key dict or `marisa-trie`; avoid SQLite `LIKE` fuzzy matching on the hot path.

### Action
Add a microbenchmark to the Phase 0/3 spike: `dict` vs SQLite-WAL vs LMDB lookup, plus the Frida RPC
round-trip — so the data-layer decision is measured, not assumed.

---

## 10. Live-translation providers — including an LLM (Claude API) provider

Upstream already ships LLM providers (`chatgpt.py`, `ollama.py`) beside DeepL/Google, so an
LLM-backed translator fits the existing pluggable-provider shape. For DQX's *dynamic* text
(uncovered NPC dialog, quests, player names), an LLM is likely the **best-quality** option —
**provided it's the Claude API (programmatic, with a key), not an interactive agent**: a real-time
game loop can't call out to a chat session.

**Why an LLM provider is attractive here**
- **Glossary-grounded consistency** — inject the curated DQX term set (item/skill/NPC/place names
  from the static DB) into the system prompt so recurring terms render identically every time.
  Generic MT (DeepL/Google) can't easily enforce this; it's the single biggest quality win.
- **Context & voice** — translate a whole dialog beat together for coherent pronouns/honorifics/
  character voice; far more natural than sentence-at-a-time MT.
- **Prompt caching** — the (large, static) glossary + style system prompt is cached, so only the
  short JA payload is billed per call. Cheap and fast after warmup.

**Costs/risks and how the architecture absorbs them**
- **Latency** (~0.3–2 s/call) and **cost/rate-limits** are real, but the cache-aside design (§9)
  means each unique string is translated **once ever**, then served from the in-memory/DB cache.
  Steady-state cost and latency trend toward zero as caches warm.
- **Cache-stability** — call with **temperature 0** and persist results, so a string's translation
  is fixed forever after first sight (a translation cache wants determinism).

**Recommended shape (Phase 3)**
- Add a `claude` provider to the provider abstraction. Default to **Haiku** on the hot dialog path
  (cost/latency); optionally **Sonnet/Opus** for cutscenes/story where quality matters most. Use
  prompt caching for the glossary, batch the queue, temperature 0, persist to SQLite.
- Keep providers pluggable: **Claude API** (best quality, needs key) / **Ollama** (offline, free,
  needs local model) / **DeepL/Google** (fast, cheap fallback). Optionally tier by content type:
  curated DB or fast MT for short UI/names, Claude for narrative.
- **Offline corpus pre-translation** (separate, legitimate use of an agent/batch): use the Claude
  API in *batch* to pre-translate the untranslated JSON corpus into the static DB ahead of time —
  improving the curated tier so less hits the live path at all.
- When we build this, the **`claude-api` skill** covers the implementation details (prompt caching,
  model selection, batching). Not invoked now — this is Phase 3.
