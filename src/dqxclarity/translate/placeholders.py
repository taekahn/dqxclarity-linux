"""Player/sibling name <-> placeholder substitution.

Community text stores the player's name as a placeholder so a single entry matches for every
player. To use those entries we swap the player's *Japanese* name into the placeholder before
lookup, then swap the player's *English* name back into the result.

There are TWO placeholder conventions in our corpus:
  * dialogue corpus:            ``<pnplacehold>`` (player) / ``<snplacehold>`` (sibling)
  * quest/event/system corpus:  ``<pc>`` (player) / ``<kyodai>`` (sibling)

``to_placeholders``/``from_placeholders`` take ``pn``/``sn`` keyword args so a caller can pick the
convention; the defaults are the dialogue tokens so existing callers/tests are unaffected.
"""

from __future__ import annotations

PN = "<pnplacehold>"
SN = "<snplacehold>"

# quest/event/system corpus tokens
PC = "<pc>"
KYODAI = "<kyodai>"


def to_placeholders(
    text: str, player_ja: str, sibling_ja: str, *, pn: str = PN, sn: str = SN
) -> str:
    """Replace the player's/sibling's Japanese name with placeholders (for lookup keys)."""
    if sibling_ja:
        text = text.replace(sibling_ja, sn)
    if player_ja:
        text = text.replace(player_ja, pn)
    return text


def from_placeholders(
    text: str, player_en: str, sibling_en: str, *, pn: str = PN, sn: str = SN
) -> str:
    """Replace placeholders with the player's/sibling's English name (for results)."""
    if player_en:
        text = text.replace(pn, player_en)
    if sibling_en:
        text = text.replace(sn, sibling_en)
    return text
