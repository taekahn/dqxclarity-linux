# IT25 Test-Writer Record — Name-Shield (GAP #25)

Three PRE-EXISTING honorific tests asserted the *old* intermediate MT-input string, where the
bare player name `タイカン` reached the provider unshielded. The GAP #25 name-shield fix now
wraps that bare name in the MT-proof EN-name sentinel BEFORE it reaches the provider — which is
the entire purpose of the fix. The tests' behavioral intent ("honorific さま is dropped before
MT") is UNCHANGED and still asserted; only the intermediate string they pin moved from the raw
name to the shielded name.

Adjustments (non-material — same positive structure, same honorific-strip intent):

1. tests/test_pipeline_integration.py :: test_translate_now_strips_name_honorific_before_mt
   - Added `t.player_name_en = "Taikan"` (the EN name the shield wraps).
   - `seen == ["タイカン、こんにちは"]` -> `seen == [f"{shield_name('Taikan')}、こんにちは"]`.

2. tests/test_mt_output_polish.py :: test_translate_now_strips_honorific_before_provider
   - Added `t.player_name_en = "Taikan"`.
   - `prov.seen == ["タイカン"]` -> `prov.seen == [shield_name("Taikan")]`.

3. tests/test_mt_output_polish.py :: test_run_strips_honorific_before_upgrade_provider
   - Added `t.player_name_en = "Taikan"`.
   - `upgrade.seen == ["タイカン"]` -> `upgrade.seen == [shield_name("Taikan")]`.

In all three, さま is still asserted gone (the honorific-strip contract). The only new
assertion is that the now-bare name is additionally shielded — the correct post-fix behavior.
No positive/negative structure changed; no test was weakened or deleted.
