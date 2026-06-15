# Memory layout & name-scanner — reverse-engineering findings

A **living** record of what we know about DQX's process memory, the name-buffer data structures, and
why the polling name scanner causes "running around" lag. Update this as new information lands (add to
the Update log at the bottom).

> Game: Dragon Quest X, 32-bit (WOW64) under Steam/Proton. Module `DQXGame.exe` loads at a fixed base
> `0x400000` (no ASLR on the main module). All measurements below are from a live `process_vm_readv`
> probe; exact numbers vary with game state.

---

## 1. Process memory layout

- **~1,533 mapped regions, ~5.2 GB** of address space total.
- **~272–275 are writable data regions (~1 GB)** — this is what a *full* name-scan sweep reads.
- **Two anonymous `rwxp` arenas dominate: ~403 MB + ~241 MB = ~64% of the swept bytes.** These are the
  game's big heap/object pools (textures, models, audio, game-object structs). A 58-byte party-name
  record essentially never lives here.
- The other ~270 regions are a **long tail of small heaps** (median region ≈ 40 KB).
- **The entire game heap is `rwxp`** (read/write/**execute**) — unusual, and the reason we can place
  detour code caves in it *and* why scans must cover executable arenas.
- `DQXGame.exe` module span: `0x400000 – ~0x27a6000` (~36 MB). Heap/object regions sit far above it
  (`0x02xxxxxx`–`0x3xxxxxxx`).

## 2. The `@D` text object — and a load-bearing gotcha

Most game text (dialogue, the login notice, chat input, etc.) is stored in one container shape:

```
[capacity : u32][HEADER : 4 bytes][UTF-8 text][00]
```

`text = header_addr + 4`, `capacity = header_addr - 4`. Null-terminated. Tens of thousands live at
once when the world is loaded (counted **84,063** in one session).

**CRITICAL:** the 4-byte HEADER is **NOT a stable magic constant — it's a per-launch pointer**
(vtable / type-object). Evidence: the value `40 44 d8 02` had **84,063 hits one session and 0 the
next**, with abundant Japanese text resident both times (the game had relaunched → new pid → the
pointer moved). A recycled buffer was also seen with header `f0 b3 d7 02`.

Consequences:
- **`send-text` (#33) and the notice scanner hardcode `CHAT_STRING_HEADER = 40 44 d8 02`** — that
  value is session-specific. They work in the session it was found, but will **silently find nothing
  on a different game launch**. → Needs to re-derive the header at runtime (e.g. from the capacity
  struct around the typed sentinel) instead of a fixed constant. *(See task — fix send-text header.)*
- The name scanner does **not** depend on this header (its patterns anchor on record layout, below),
  so it is unaffected.

## 3. The name scanner — what it scans, and why it lags

Names are **not** delivered through a hookable formatting function, so they can't be detoured like
dialogue. They sit in game-object records that the scanner finds by AOB pattern and overwrites. The
three patterns (`src/dqxclarity/process/signatures.py` → `NAME_PATTERNS`):

| pattern          | what it backs                          | name offset | notes |
|------------------|----------------------------------------|-------------|-------|
| `menu_ai_name`   | **support-companion / party-member** name records (the HUD member list) | +57 | 58-byte struct: `01 00 00 00` + zero run + field bytes + JA lead byte |
| `comm_name`      | **chat sender** names                  | +0  | name first, then fixed trailer `00 00 0F 00 00 00 01 02 00 00 01 00 00` |
| `concierge_name` | town **service NPCs** (bazaar/quest)   | +12 | `D8 E5 …… 68 0C` markers |

Two structural facts that drive the lag:

1. **Every pattern ends in a Japanese UTF-8 lead byte `[\xE3–\xE9, \xEF]`** — so a pattern **only
   matches a name that is still Japanese**. The instant the scanner translates a name to ASCII
   English, that buffer **stops matching its own pattern**.
2. The patterns are **contextual UI records**, not overworld content. Field-NPC nameplates floating
   over people are translated by the **nameplates *hook*** (event-driven, ~free) — NOT the scanner.

### Root cause of "running around" lag (confirmed)

While running around with everything already translated, **all 3 patterns return 0 matches** — not
because there's nothing, but because the names are **already English and no longer match the
Japanese-anchored patterns**. The scanner can't see its own results, so its warm-region cache empties
and it falls back to a **blind ~1 GB full sweep every `FULL_RESCAN_SECS` (20 s)** — doing expensive
discovery for work that is already done. That periodic blind sweep is the micro-stutter.

(`menu_ai_name`'s `01 00 00 00`+16-zero prologue alone matches **20,000+** places in memory, so a
cheap prologue prefilter does NOT help — the specificity comes from the later field bytes + JA lead
byte.)

### Warm-region optimization (already shipped, commit 63a051a)

- **Maintenance tick** (most ticks, ~1 s): rescan only regions that yielded a hit last pass (1–3 small
  buffers). Cheap.
- **Discovery sweep** (every 20 s, or on warm-empty/zone-change, with a backoff): full ~1 GB scan to
  find name records in new regions. **This is the expensive tick.**
- The "patterns match only Japanese" fact (above) means translating the last name in a warm region
  empties the warm set → triggers rediscovery → the scanner partly fights its own success.

## 4. Where the data comes from + lifecycle

- **Party/companion (`menu_ai_name`) and chat (`comm_name`) names are server-delivered** — they arrive
  over the Blowfish-encrypted network, get decrypted, and land in these heap records when the UI is
  built.
- **Concierge/NPC names** come from client game data, instantiated when you're near the NPC.
- **Records are allocated when their UI/content appears and freed when it goes away** (panel closes,
  NPC despawns, zone change). Re-creation = **new address** every time → why we can't cache a pointer
  and must re-discover; also why the buffer relocates between sightings.

## 5. Terminology note — "party panel"

Ambiguous; pin down per use. `menu_ai_name` (`ai` = support/AI companions, サポート仲間) backs the
**party-member name display** (the HUD list of your party with names/HP/MP — the source of the earlier
"Squid MRT / Kimaana WAR" overhead nameplates), not necessarily a menu you open. When discussing
"which UI is active" for context-gating discovery, be specific about which surface.

## 6. Lag — solution directions (grounded in the above)

The problem is **when/where we sweep**, not raw sweep speed: we sweep ~1 GB constantly for content
that is usually absent or already done.

1. **Remember translated addresses.** After translating a name at addr X, keep cheaply re-reading X
   (it stays English until freed/reallocated) instead of relying on the pattern to re-find it. This
   stops the "translated → pattern misses → blind sweep" loop. New names still need discovery, but we
   stop sweeping merely because our results turned English.
2. **Aggressive/exponential discovery backoff.** Each consecutive sweep that finds nothing pushes the
   next out (20 → 40 → 80 → cap), reset instantly when any name appears. Overworld settles to a sweep
   every minute-plus. Cheap, low risk; new names take a little longer to first appear.
3. **Skip the giant arenas / size-class filter.** Name records never live in the 403 MB / 241 MB
   pools; exclude regions over a size threshold (or only ever-sweep regions that have yielded a name
   hit this session). Cuts any sweep that does run by ~5×.
4. **Context-gate discovery on a UI signal** (elegant endgame): only sweep `menu_ai_name` when the
   party HUD is active, `comm_name` when chat is flowing — if we can find a "panel/chat active" flag
   or piggyback on a hook firing. Eliminates idle sweeps.
5. **Amortize** (#34): spread any remaining sweep across N ticks so it's never a single spike.
6. **Two-stage / cheaper match** for whatever we do scan.

Current lean: **#1 + #2 (+ #3)** kill the overworld lag with low complexity; **#4** is the endgame.

## 7. Open questions / to measure

- Exactly which regions + size-class the **live Japanese** name records occupy (need them resident &
  untranslated to capture — i.e. probe right as a panel/NPC/chat first appears, before translation).
- Is there a findable **UI-state flag** (party HUD active / chat active / zone id) to gate discovery?
- The current-session value of the `@D` header (to re-derive it at runtime for send-text/notice).
- Do `menu_ai_name` records persist while running around (with a party) or only while a specific
  surface is open? ("all names translated" suggests they were present and got translated.)

---

## Update log

- **2026-06-15** — Initial findings. Region makeup (1 GB swept, 2 arenas = 64%); `@D` header is a
  per-launch pointer (84k→0 across relaunch) → send-text/notice fragility; name patterns match only
  Japanese → confirmed root cause of running-around lag is **blind sweeps after everything is already
  translated** (user confirmed all on-screen names were translated). Solution directions captured.
