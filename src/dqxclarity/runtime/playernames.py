"""Apply auto-detected player/sibling names from the PLAYER login hook.

The PLAYER hook (``process.detour.PlayerHook``) reads the player's and sibling's Japanese names out
of the game's login struct (read-only) and calls the ``apply_names`` callback built here. We:

  * romanize each non-empty Japanese name to an English name (offline, via pykakasi), so the
    community-DB placeholder swap (<pnplacehold>/<snplacehold>, <pc>/<kyodai>) can put a real
    English name back into a curated line;
  * update the LIVE translator names so the next community lookup uses them WITHOUT a restart
    (``dispatch._make_community_lookup`` reads ``translator.player_name_*`` on every call);
  * mirror them into ``cfg.translate.*`` and persist once via ``config.save`` so the detection
    survives a restart too.

It is idempotent and cheap: if the detected names already match what's set, it does nothing (no
redundant disk write every login frame) and returns ``None`` so the caller logs only on a real
change. This module performs NO writes into the game's memory — it only updates our own
config/translator state.
"""

from __future__ import annotations

from .. import config as config_mod
from ..translate import romanize


def _resolve_en(ja: str) -> str:
    """Romanize a Japanese name to an English one, guarded by pykakasi availability.

    Returns "" for an empty input; falls back to the Japanese name if no romanizer is available so
    the EN slot is never silently blanked when JA is present.
    """
    if not ja:
        return ""
    if romanize.is_available():
        return romanize.romanize(ja)
    return ja  # no romanizer — keep the JA so the EN slot isn't blank


def build_apply_names(cfg, translator, *, save=None):
    """Return ``apply_names(player_ja, sibling_ja, relationship)`` for the PLAYER hook.

    ``apply_names`` romanizes the names, updates the live translator + the config, and persists the
    config once (via ``save`` or ``config.save``). It is idempotent and signals "changed" cleanly:
    it returns the resolved ``(player_en, sibling_en)`` ONLY when the names actually changed (so the
    caller can log the detection exactly once); when the NAMES are unchanged it returns ``None`` and
    does NOT re-save.

    The idempotency check is on the NAMES ONLY — ``sibling_relationship`` is deliberately excluded
    because it is NOT persisted to config, so after a restart it resets to 0 and would otherwise
    force a spurious re-save (and a duplicate "detected" log) on the very next login. The
    relationship is still captured onto the translator for potential future use, but never drives
    idempotency or saving.
    """
    save_fn = save if save is not None else config_mod.save

    def apply_names(player_ja: str, sibling_ja: str, relationship: int):
        player_ja = player_ja or ""
        sibling_ja = sibling_ja or ""

        player_en = _resolve_en(player_ja)
        sibling_en = _resolve_en(sibling_ja)

        # Idempotency on the NAMES ONLY (JA + romanized EN). relationship is intentionally excluded:
        # it isn't persisted, so it resets to 0 on restart and would force a spurious re-save/log
        # every login. Capture it onto the translator regardless (future use), but don't compare it.
        translator.sibling_relationship = relationship
        if (
            translator.player_name_ja == player_ja
            and translator.player_name_en == player_en
            and translator.sibling_name_ja == sibling_ja
            and translator.sibling_name_en == sibling_en
        ):
            # Nothing changed since the last login -> no save, and signal "no change" with None so
            # the caller's on_line/log fires only on a real change.
            return None

        # Update the LIVE translator first so the next community lookup uses the new names even if
        # the (disk) save below fails.
        translator.player_name_ja = player_ja
        translator.player_name_en = player_en
        translator.sibling_name_ja = sibling_ja
        translator.sibling_name_en = sibling_en

        cfg.translate.player_name_ja = player_ja
        cfg.translate.player_name_en = player_en
        cfg.translate.sibling_name_ja = sibling_ja
        cfg.translate.sibling_name_en = sibling_en
        try:
            save_fn(cfg)
        except OSError:
            pass  # a config-write failure must never break the (already-applied) live update

        return player_en, sibling_en

    return apply_names
