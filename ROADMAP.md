# dqxclarity-linux — Roadmap (revised)

Linux-native, CLI-only fork of dqxclarity that translates **Dragon Quest X Online** under
Steam/Proton. Two layers: **static file patching** (curated archives/exes) and **live in-memory
translation** (community DB → fast MT) injected via native x86 code-cave detours (no Frida).

Last revised after closing the upstream-parity gap audit and the data/MT-quality batches.

---

## ✅ Done — the engine and the surfaces are complete and verified live

**Native runtime**
- `process_vm_readv/writev` memory R/W on the WOW64 game (no ptrace/elevation).
- Code-cave detours: **blocking** hook, **return** hook (entry+exit + shadow stack), read-only
  **player** hook. Crash-recovery **hook journal** + signal-safe restore (`run` survives
  SIGTERM/SIGKILL without orphaning hooks; `clean` command).

**The full hook fleet (7 surfaces, all live + verified)**
| Surface | What it translates |
|---|---|
| dialogue | NPC/cutscene dialogue boxes |
| quest | quest log (name / desc / rewards struct) |
| walkthrough | "where to go" directions |
| corner_text | top-right NPC text |
| nameplates | overhead entity names (with `\x04` so they don't render red) |
| network_text | Story So Far recap + NPC names + quest template strings (category-filtered) |
| player | auto-detects player **and sibling** names on login, applied live |

**Translation pipeline**
- Quality-ranked cache (community > MT) → community/human first, then fast Google MT first-view.
- Both player-name placeholder conventions (`<pnplacehold>`/`<snplacehold>` **and** `<pc>`/`<kyodai>`).
- **Static corpus**: 208k dqx_translations + merge.xlsx (4 sheets, incl. Story So Far **DeepL
  fallback**) + **wholesale** custom-JSON glob.
- **Glossary** (~40k terms) applied to every MT'd line for proper-noun consistency.
- **MT polish**: post-MT char fixes (curly quotes/dashes/ellipsis/accents → ASCII the font renders),
  honorific stripping after the known name (no more "Taikan-sama").
- Display correctness: per-surface wrap/pagination profiles, inline `<color_x>` tag preservation,
  Story So Far panel fit + cutoff marker.

The audit's worst class of bug — text that silently falls to MT because we imported a *subset* of an
upstream source — is closed (custom JSON, DeepL recaps, glossary all now whole-source).

---

## 🔜 Remaining work

### Phase A — Translation quality completeness (finish the curated layer)
- **#21** Quest reward fields: per-field item-list cleanup (item-name lookup + `(N)` quantity +
  `討伐ポイント→Experience Points`), instead of whole-string MT that mangles the list.
- **#23** BAD STRING suppression: key the curated bad-output fixes correctly + a substring pass so
  known game-breaking MT outputs can't resurface.
- **#14** Tag-protection swap (40+ variable tags) before MT, incl. the deferred `<kyodai_rel*>`
  sibling-relationship tag (we already capture the relationship byte from the player hook).

### Phase B — Currency & sharing (the endgame: "sit down to a new game with perfect translations")
- **#19** Auto-refresh the translation DB on launch (+ a `doctor` staleness signal) so users aren't
  frozen on an old corpus.
- **#20** Shareable DB **export** (re-placeholdered, by quality tier) + community **submission**
  (POST-back) so Linux users contribute, not just consume.
- **Re-enable the background quality provider** (Claude `upgrade_provider`, currently off): async so
  it never causes an open-panel pause; it upgrades cache entries to high quality and feeds the
  shareable DB. This is the original layered-pipeline vision's capstone.

### Phase C — Robustness & polish
- **#16** Graceful service exit when the game closes (no traceback).
- **#12** network_text translate-all-Japanese — only once it's perf-safe and cache-warm.
- Tooling: name_overrides (user name pins for disliked romanizations), version/signature-drift
  check in `doctor`.

---

## Recommended sequence
Phase A first (make the translations as good as possible **before** we share them), then Phase B
(share + keep current — the stated endgame), with Phase C polish folded in opportunistically. Each
item is independently shippable and lands as its own committed increment with adversarial review.
