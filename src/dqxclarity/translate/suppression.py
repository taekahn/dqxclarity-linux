"""BAD STRING suppression — return a curated English fallback for known-broken machine inputs.

Some Japanese strings, when machine-translated, break the game or produce a confusing experience
(the player info was redacted when they were first collected, so they live in the corpus as PARTIAL
strings). Upstream stores these in a ``bad_strings`` table and checks it FIRST in the dialogue
pipeline — BEFORE the cache/MT — returning the curated ``en`` when the bad ja is a SUBSTRING of the
captured text (``ja in text``, a contains/substring match, NOT an exact match). See
``search_bad_strings`` (dqxclarity/app/common/db_ops.py:176-202) and the dialogue hook ordering
(dqxclarity/app/hooking/hooks/dialogue.py:39-42).

This module ports that behavior. ``SuppressionIndex`` is built from the bad_string=1 entries that
``community.load_suppressions`` extracts from merge.xlsx and exposes a single ``match(text)`` that
returns the fallback ``en`` (or None). It is name/placeholder-aware: a suppression keyed with a name
placeholder (``<pc>``/``<pnplacehold>``) still matches captured text that contains the LITERAL live
player/sibling name, and vice-versa — consistent with how ``dispatch._make_community_lookup`` swaps
the live name <-> placeholder for the cache lookup.
"""

from __future__ import annotations

from .placeholders import KYODAI, PC, PN, SN, from_placeholders, to_placeholders

# The same two placeholder conventions dispatch.CONVENTIONS uses, in the same order: dialogue corpus
# (<pnplacehold>/<snplacehold>) first, then quest/event/system corpus (<pc>/<kyodai>).
_CONVENTIONS = ((PN, SN), (PC, KYODAI))


class SuppressionIndex:
    """Substring-matching index of BAD STRING suppression entries (mirrors upstream search_bad_strings).

    Built from ``(ja_key, en)`` pairs. ``match(text)`` returns the first entry's ``en`` whose ``ja``
    is a SUBSTRING of ``text`` (upstream ``ja in text``), trying the live-name <-> placeholder swap in
    both directions so a placeholder-keyed entry matches literal-name text and vice-versa. Entries are
    kept in insertion order; an empty key never matches (an empty string is a substring of everything,
    which would suppress all text). The match is read-only and allocation-light on the hot path.
    """

    def __init__(self, entries: list[tuple[str, str]]):
        # Keep insertion order (first match wins, like upstream's row scan); drop empty keys so an
        # empty "" can't spuriously match every string.
        self._entries: list[tuple[str, str]] = [(ja, en) for ja, en in entries if ja]

    def __len__(self) -> int:
        return len(self._entries)

    def match(
        self,
        text: str,
        *,
        player_ja: str = "",
        player_en: str = "",
        sibling_ja: str = "",
        sibling_en: str = "",
    ) -> str | None:
        """Return the curated EN for the first suppression entry that is a SUBSTRING of ``text``.

        Tries, for each entry and each placeholder convention, three alignments of key vs. text:
          1. the raw key in the raw text (the no-name case, and the already-aligned case);
          2. a PLACEHOLDER-keyed entry vs. literal-name text: placeholder the text (live JA names ->
             placeholders) and check the raw key in it;
          3. a LITERAL-name-keyed entry vs. placeholder text: de-placeholder the text (its
             placeholders -> live JA names) and check the raw key in it.
        On a hit the returned EN has its placeholders swapped to the player's/sibling's ENGLISH name
        (``from_placeholders``), so a fallback keyed with ``<pc>`` renders the live English name.
        Returns None when nothing matches.
        """
        # Pre-compute, once per convention, the placeholdered + de-placeholdered copies of the text.
        variants: list[tuple[str, str, str, str]] = []
        for pn, sn in _CONVENTIONS:
            ptext = to_placeholders(text, player_ja, sibling_ja, pn=pn, sn=sn)        # case 2
            dtext = from_placeholders(text, player_ja, sibling_ja, pn=pn, sn=sn)      # case 3
            variants.append((pn, sn, ptext, dtext))

        for ja, en in self._entries:
            # Case 1: raw contains (covers no-name entries and already-aligned text).
            if ja in text:
                return self._render(en, player_en, sibling_en)
            for pn, sn, ptext, dtext in variants:
                has_ph = pn in ja or sn in ja
                # Case 2: placeholder-keyed entry vs. literal-name text (text placeholdered to match).
                if has_ph and ptext != text and ja in ptext:
                    return self._render(en, player_en, sibling_en, pn=pn, sn=sn)
                # Case 3: literal-name-keyed entry vs. placeholder text (text de-placeholdered to match).
                if not has_ph and dtext != text and ja in dtext:
                    return self._render(en, player_en, sibling_en, pn=pn, sn=sn)
        return None

    @staticmethod
    def _render(en: str, player_en: str, sibling_en: str, *, pn: str = PN, sn: str = SN) -> str:
        """Swap any name placeholders in the fallback EN to the live ENGLISH name(s)."""
        return from_placeholders(en, player_en, sibling_en, pn=pn, sn=sn)
