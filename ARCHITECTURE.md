# dqxclarity-linux — Architecture & Forward Plan

A living architectural overview, written at the first natural checkpoint (dialogue translation
working end-to-end). It captures **what we built**, **what we learned (incl. disproven
assumptions)**, the **invariants that must hold**, and the **target architecture** for the future
goals: cover the whole screen, a layered translation pipeline that ends in a high-quality
Claude-built **shareable** database, and proper-name preservation — all without breaking the
on-screen display or the hot path.

---

## 1. One-paragraph summary

DQX runs as a 32-bit Windows game under Steam/Proton (Wine WOW64). Because Wine runs the game's
x86 natively, the game is just a Linux process: we read/write its memory with
`process_vm_readv/writev` and inject x86 **detour hooks** into its text functions. Each text
surface (dialogue, quests, names, …) is intercepted, the Japanese is resolved through a layered
**translation pipeline** (human community data → fast machine translation → background
high-quality upgrade), and English is written back into the game's buffer before it renders.
Plus static **file patching** for the baked-in UI. No Windows, no Frida, no metered API required.

---

## 2. Current architecture (as built)

```
   ┌─────────────────────── the game (DQXGame.exe, 32-bit, under Proton) ───────────────────────┐
   │  text surfaces:   dialogue ✓   quests ✗   nameplates ✗   walkthrough ✗   corner ✗   chat ✗  │
   └───────────────▲───────────────────────────────────────────────────────────────────────────┘
                   │ native x86 detour (blocking): pause thread → translate → write EN → resume
   ┌───────────────┴───────────────┐         ┌──────────────── static file patching ───────────┐
   │  process/  (per-process I/O)  │         │  patching/ : download modded .dat/.idx + exes     │
   │   discover  memory_linux      │         │  (the baked-in menus/UI that ship in game files)  │
   │   detour    signatures        │         └──────────────────────────────────────────────────┘
   └───────────────┬───────────────┘
                   │ JA string in / EN string out
   ┌───────────────┴────────────────────── translate/ (the pipeline) ──────────────────────────┐
   │  pipeline.Translator  ── lookup (hot) ──► db.TranslationCache (in-mem dict + SQLite WAL)    │
   │     │ community-first → fast sync MT → background quality upgrade (quality-ranked cache)    │
   │     ├─ dialogue.py   (tag-preserving, <select>, wrapping)                                   │
   │     ├─ wrap.py       (46-wide, <br> every 3 lines, strip 「」, normalize)                    │
   │     ├─ placeholders  (player/sibling name <-> <pnplacehold>)                                │
   │     ├─ community.py  (sync the curated merge.xlsx dataset)                                  │
   │     ├─ romanize.py   (local pykakasi for names)                                             │
   │     └─ providers/    (googletranslatefree [fast], claude_cli [quality], …)                  │
   └────────────────────────────────────────────────────────────────────────────────────────────┘
   cli.py · config.py (TOML) · doctor.py · runtime/names_loop.py
```

**Two translation mechanisms:**
- **Static (file patching):** download community-modded `data00000000.win32.{dat1,idx}` + translated
  `DQXConfig.exe`/`DQXLauncher.exe`. Covers the baked-in UI. Reversible, idempotent.
- **Live (memory hooks):** intercept server-delivered text at render time and replace it. Currently
  only the **dialogue** function is hooked; **names** use a polling scanner.

---

## 3. Lessons — disproven assumptions

Recording these because they shaped the architecture and prevent backsliding:

1. **"Frida will let us reuse upstream's hooks."** ❌ Frida cannot attach to the Proton **WOW64**
   process (`ptrace pokedata: I/O error`). → We built native x86 detours instead. *(Don't retry
   Frida on WOW64.)*
2. **"Memory access needs elevation / a launcher-parent trick."** ❌ `process_vm_readv/writev` work
   with zero elevation despite `ptrace_scope=1`. Hooking needs no ptrace at all (the game's own
   thread runs our injected code).
3. **"The disappearing text is an async-vs-sync problem."** ❌ It was **bugs**: dropped control
   codes (`<close>`/`<wait>`), leftover stale JA bytes, unstripped `「`, mis-pagination. Synchronous
   writes help secondarily (no JA-layout-then-EN-overwrite mismatch), but the fixes were the cause.
4. **"Clean dialogue = good machine translation."** ❌ Upstream's clean dialogue is **human
   community data**, not MT. The community API is *submit-only*; the corpus ships as files
   (`merge.xlsx`, 4k+ lines, `<pnplacehold>` player-name slots).
5. **"claude_cli as the live provider."** ❌ Too slow (~2-4s) for first-view → everything showed
   Japanese. Fast synchronous MT (free Google ~200ms) is required on the hot path; Claude belongs in
   the **background** quality tier.
6. **"The community dataset covers the player's content."** ⚠️ Partially — the shipped DB is a
   ~2.6k-row subset; `merge.xlsx` adds ~4.3k. Coverage is real but incomplete, which is *why* the
   high-quality background tier (building a shareable DB) matters.

---

## 4. Design invariants (the rules everything else serves)

These are ranked. When they conflict, the higher one wins.

- **I1 — Never break the on-screen display (PRIME).** A perfect translation you can't read is worse
  than a bad one you can. Concretely: never overflow the game's text buffer; always preserve control
  codes (`<close>`, `<wait>`, `<select>`, `<br>`); always wrap/paginate to the box; if a translation
  can't be made to fit/format safely, fall back (shorter text, or leave Japanese) rather than
  corrupt the display. **This must be enforced by tests, not vigilance.**
- **I2 — The hot path stays fast.** The game thread is *blocked* during a hook. Only allowed work on
  it: in-memory cache lookup (µs) and *at most* one fast synchronous MT call (~200ms, bounded by the
  spin-timeout). Everything slow (Claude, batch, self-critique) runs on a **background thread** and
  only *upgrades the cache* — never blocks the game.
- **I3 — Quality only increases.** Cache entries are quality-ranked; a lower tier never overwrites a
  higher one (`store_if_better`). Human/community/curated > Claude > Google > romaji.
- **I4 — Injection is safe and reversible.** Save original bytes; restore on exit; a dead Python
  process must not freeze the game (spin-timeout). Use only the game image's own writable-executable
  caves. Operate on a backup install while iterating.
- **I5 — Proper names stay intact.** Names must survive translation unmangled (see §6).

---

## 5. The translation pipeline — current and target

### Resolution order (hot path, per intercepted string)

```
1. STATIC / COMMUNITY  (in-memory dict; human-curated; instant)   ─┐ highest quality
2. CACHED upgrade      (Claude-built or prior result; instant)     │ if present, use it
3. FAST DYNAMIC        (free Google, synchronous ~200ms)           │ instant first-view
                          → write EN now, AND enqueue ↓            │
─────────────────────────────────────────────────────────────────┘
4. BACKGROUND QUALITY  (Claude — takes all the time it needs)      ── upgrades the cache (I3),
                          off the hot path (I2)                        shows on next view
```

This is the **three layers you described**, made precise:
- **Layer 1 — static/community** (file patch + synced `community` rows). Human quality, instant.
- **Layer 2 — fast dynamic** (Google now; pluggable). Instant first-view so nothing is ever stuck
  in Japanese.
- **Layer 3 — high-quality curated** (Claude). Background only. Its job is to *build* a corpus that
  **rivals and complements** the human community data, so coverage trends toward "perfect" over time.

### The background quality engine (Layer 3) — where the future work concentrates

Because Layer 3 is off the hot path, it can be as sophisticated and slow as we like (I2). Planned:

- **Providers — `claude_api` PRIMARY, `claude_cli` fallback.** The API is faster (no per-call CLI
  boot), reliable, and supports **prompt caching** (cache the big system prompt/glossary) + the
  **Batch API** (50% cost, ideal for cheap bulk corpus-building). Since this tier is bounded
  (each unique line translated once, off the hot path) the metered cost is small and one-time —
  the earlier "no metered drain" rule was about the *live hot path*, which stays free (Google).
  `claude_cli` (subscription, no metered cost) is the **fallback** when no API key is set or the
  API is unavailable. Selection: API if a key is configured → else CLI.
- **Context-aware translation** (the "sophisticated tier"): feed Claude the NPC name, the
  surrounding conversation, the **proper-noun glossary**, and prior translations of the same
  speaker — so it renders consistent, in-character English. Optionally a self-critique / second pass.
- **Beyond context — advanced quality techniques (exploration / polish).** Worth investigating for
  *both* API and CLI: **personas** (a translation voice per speaker/archetype — a gruff soldier vs a
  courtly NPC — for consistent character voice); **agentic translation** (an agent that can look up
  lore/wiki, prior translations, and the glossary, then reason and refine); reusable **skills**
  (honorific handling, pun/wordplay localization, name-reading rules); **multi-pass** translate →
  critique → refine; and lore/world grounding. These raise quality of the corpus we build; deferred
  as polish but recorded as a direction.
- **Determinism for cache stability:** temperature 0; every result cached and ranked.
- **The shareable DB (the end goal):** the high-quality entries Claude builds are exactly the form
  of the community dataset (`ja → en`, with `<pnplacehold>` and control codes preserved). So they
  can be **exported and contributed back** — `dqxclarity export-translations` → merge into the
  project corpus / a community store. Over time this converges on the vision: *someone sits down to
  a fresh game and it's already fully, well translated.* The local cache **is** the seed of that
  shared corpus.

### Performance model (why Layer 3 doesn't hurt)

The only network call on the blocked game thread is Layer 3? **No** — Layer 3 is background. The hot
path does: dict lookup (µs) → on miss, one Google call (~200ms, short HTTP timeout, spin-timeout
fallback to JA) → write. Claude/batch/self-critique never touch the game thread. As the cache (and
shared corpus) fills, even Layer 2 calls shrink toward zero — steady-state is mostly Layer 1/2
cache hits.

---

## 6. Proper-name preservation (I5)

Every MT pass can mangle proper nouns (NPC, place, monster, item, player/sibling names) → confusion.
We already protect **player/sibling** names via `<pnplacehold>`/`<snplacehold>`. Generalize:

- **A proper-noun glossary** built from the community data we already pull (the NPC-name, monster,
  item, place JSON files) → a `{japanese_name: canonical_english}` lexicon.
- **Fast tier (Google):** *placeholder-protect* — before sending, swap known proper nouns for inert
  tokens MT won't touch; restore the canonical English after. (Same trick as player names.)
- **Quality tier (Claude):** *glossary injection* — give Claude the relevant glossary entries in the
  prompt so it uses canonical names natively (and can disambiguate by context).
- **Guardrail:** a post-translation check that flags/repairs known names that came out wrong.
- This lives in a `translate/glossary.py` + `proper_nouns` lexicon, tested with round-trip cases.

---

## 7. Covering the whole screen — generalize the hook

Right now the dialogue hook is bespoke. The remaining surfaces (quests, nameplates, walkthrough,
corner text, chat) are *the same shape*: find a function signature, detour it, pull the text arg,
run the pipeline, write back. So the refactor is to make hooks **declarative**:

```python
@dataclass
class HookSpec:
    name: str                 # "dialogue" | "quest" | "nameplate" | ...
    signature: bytes          # AOB of the function prologue
    stolen_len: int
    text_arg: ArgRef          # where the JA text pointer is (stack offset / register)
    writeback: WritebackPolicy  # in-place bounded write; format profile (box width, page lines)
    pipeline: str             # which formatting profile (dialogue vs short-label vs menu)
```

- A registry of `HookSpec`s; `dqxclarity run` installs **all enabled** hooks at once and drives one
  serve loop.
- Per-surface **format profiles** (dialogue wraps 46/3; a quest title might be one short line; a
  nameplate is a tiny fixed field — each has its own width/▢-fit rules under I1).
- Signatures for quests/nameplates/etc. come from upstream's `hooking/scripts/*.ts` (ported like the
  dialogue one). Their translations are *already in our cache* (the `sync` imported `quests` etc.).

This turns "translate another surface" into "add a HookSpec + its signature," which is the path to
covering the whole screen.

---

## 8. Testing strategy (lock it down — I1/I3 especially)

The user is right to want tests as the safety net. Priorities:

- **Display-safety property tests (I1):** for arbitrary JA + translation, assert the written bytes
  never exceed buffer capacity, control codes are preserved, and the wrapped output respects the box
  profile. Fuzz with long/short/edge translations.
- **Quality-rank invariants (I3):** `store_if_better` never downgrades; community always wins;
  upgrades replace lower tiers. (Have basic ones; expand to all rank pairs.)
- **Thread-safety:** concurrent `translate_now` (game thread) + background upgrade worker hammering
  the cache — assert no corruption, no lost-update that downgrades.
- **Tag/format correctness:** `<select>` menus, nested tags, tag-only strings, `<br>` pagination,
  `「」` stripping, placeholder round-trips (have several; broaden).
- **Proper-noun round-trips (I5):** names survive the fast tier (placeholder) and are consistent in
  the quality tier (glossary).
- **Detour assembler:** shellcode offsets, jmp rel32 math, capacity/zero-pad logic (have the
  assembler tests; add serve_once write-back unit tests with a fake memory).
- **Provider robustness:** simulate provider failure/timeout → pipeline degrades gracefully (leaves
  JA), never raises into the game thread.

---

## 9. Phased roadmap (future)

- **P1 — Finish the screen (your priority #1).** Generalize to `HookSpec` registry; add **quests**
  (#6), then **nameplates**, **walkthrough**, **corner_text**, **network_text/chat**. Per-surface
  format profiles under I1. *(Translations largely already synced.)*
- **P2 — Proper-name layer (§6).** Glossary from community data; placeholder-protect (fast) +
  glossary-inject (quality); guardrail + tests.
- **P3 — Layer-3 quality engine (§5).** `claude_api` provider (Console key, prompt caching, **Batch
  API**) as **primary**, `claude_cli` as fallback; context-aware prompts (NPC name, history,
  glossary); temperature 0; expanded background worker. (Then, as polish: personas / agentic /
  skills / multi-pass per §5.)
- **P4 — Shareable corpus.** `export-translations` (cache → community-format), import/merge, dedup
  vs the project corpus; a path to contribute back. The "perfect on day one" endgame.
- **P5 — Hardening.** Display-safety + thread-safety + provider-failure test suites; `doctor`
  coverage; auto-detect player name (drop manual config); packaging.

Cross-cutting, always: **I1 (don't break the display)** and **I2 (don't slow the hot path)** gate
every change.

---

## 10. Open questions for the next session

1. Player-name auto-detection (read it from memory like upstream's `player` hook) vs. manual config.
2. Where the **shared corpus** lives (contribute to the existing dqx-translation-project, or a
   separate dqxclarity-linux store?) and the privacy model (player-generated content must be
   placeholder-stripped before sharing — upstream does this).
3. ~~claude_cli vs API as primary~~ — **decided: `claude_api` primary (faster, Batch API), `claude_cli`
   fallback.** Open: how far to push advanced quality (personas/agents/skills/multi-pass) and how to
   measure that it's actually improving translations (eval harness?).
4. Per-surface format profiles: do we need to measure each box's real width/line-count in-game
   (like we tuned dialogue to 46/3), or can we derive them from the community data's formatting?
