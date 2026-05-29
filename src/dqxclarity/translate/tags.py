"""Protect DQX variable/color tags from machine-translation mangling.

The game embeds dozens of ``<...>`` variable tags (player-name placeholders, suit-symbol
glyphs, title slots, color codes, sibling-relationship words) that expand to other text at
render time. If we feed those raw tags to a machine translator it happily translates,
reorders, lowercases, or sprinkles spaces inside them — and the tag stops resolving in-game.

Mirrors upstream's proven scheme (``app/common/translate.py`` —
``__swap_placeholder_tags`` table at lines 63-103 and the ``<color_(\\w+)>`` regex at ~325):
we swap each known tag to a short ASCII *sentinel* (e.g. ``<&13_aaaaaaa>``) that survives MT
intact, run the translator, then swap the sentinels back. The sentinels are deliberately
opaque ASCII so the translator leaves them alone.

These functions are PURE (no Translator import) so they're unit-testable in isolation. The
pipeline calls :func:`protect_tags` right after honorific-stripping (before glossify/MT) and
:func:`restore_tags` on the MT output.

The ``<kyodai_rel1/2/3>`` sibling-relationship tags get special treatment: instead of a
round-trip sentinel they resolve straight to the correct *English* word
("brother"/"sister"/"sibling") based on the logged-in player's relationship byte, protected
behind a sentinel so MT can't mangle the chosen word either.
"""

from __future__ import annotations

import re

# Canonical tag -> sentinel table. Mirrors upstream's __swap_placeholder_tags forward branch
# (translate.py:64-103) EXACTLY: same tags, same sentinels. The sentinels are chosen to be
# opaque ASCII that MT leaves untouched. Order matters for substring-prefix tags — longer /
# more-specific tags MUST come before their prefixes (e.g. <pc_hiryu> before <pc>, <kyodai_rel1>
# before <kyodai>) so a .replace() of the short tag never clobbers the long one's inner text.
# <kyodai_rel1/2/3> are handled separately (resolved to an English word), so they're NOT here.
_TAG_SENTINELS: list[tuple[str, str]] = [
    ("<pc_hiryu>", "<&13_aaaaaaa>"),
    ("<cs_pchero_hiryu>", "<&13_aaaaaab>"),
    ("<cs_pchero_race>", "<&8_aaa>"),
    ("<cs_pchero>", "<&13_aaaaaac>"),
    ("<pc_hometown>", "<&8_aab>"),
    ("<pc_race>", "<&8_aac>"),
    ("<%sM_real_race>", "<&8_aad>"),
    ("<pc_rel1>", "<&7_ad>"),
    ("<pc_rel2>", "<&7_ae>"),
    ("<pc_rel3>", "<&7_af>"),
    ("<kyodai>", "<&13_aaaaaad>"),
    ("<pc>", "<&13_aaaaaae>"),
    ("<client_pcname>", "<&13_aaaaaaf>"),
    ("<heart>", "<&2a>"),
    ("<diamond>", "<&2b>"),
    ("<spade>", "<&2c>"),
    ("<clover>", "<&2d>"),
    ("<r_triangle>", "<&2e>"),
    ("<l_triangle>", "<&2f>"),
    ("<half_star>", "<&2g>"),
    ("<null_star>", "<&2h>"),
    ("<npc>", "<&13_aaaaaag>"),
    ("<pc_syokugyo>", "<&13_aaaaaah>"),
    ("<pc_original>", "<&13_aaaaaai>"),
    ("<log_pc>", "<&13_aaaaaaj>"),
    ("<%sM_NAME>", "<&13_aaaaaak>"),
    ("<%sM_BEFORE_NAME>", "<&13_aaaaaal>"),
    ("<%sM_OWNER_OTHER>", "<&13_aaaaaam>"),
    ("<%sM_OWNER>", "<&13_aaaaaan>"),
    ("<%sM_SAMA>", "<&6_a>"),
    ("<1st_title>", "<&20_aaaaaaaaaaaaaa>"),
    ("<2nd_title>", "<&20_aaaaaaaaaaaaab>"),
    ("<3rd_title>", "<&20_aaaaaaaaaaaaac>"),
    ("<4th_title>", "<&20_aaaaaaaaaaaaad>"),
    ("<5th_title>", "<&20_aaaaaaaaaaaaae>"),
    ("<6th_title>", "<&20_aaaaaaaaaaaaaf>"),
    ("<7th_title>", "<&20_aaaaaaaaaaaaag>"),
]

# Sibling-relationship tags. Upstream maps these to JA variants in the player hook
# (player.py:34-142); in our EN pipeline the correct *end result* is the English sibling word.
# We protect that word behind a dedicated sentinel so MT can't translate "brother" into another
# language or mangle it. All three sentinels share the same body so MT-typo tolerance is uniform.
_KYODAI_REL_TAGS = ("<kyodai_rel1>", "<kyodai_rel2>", "<kyodai_rel3>")
_KYODAI_REL_SENTINEL = "<&7_aa>"  # matches upstream's <kyodai_rel1> sentinel body

# relationship byte -> English sibling word. 1/2 = brother, 3/4 = sister, anything else = sibling.
_SIBLING_WORD = {1: "brother", 2: "brother", 3: "sister", 4: "sister"}
_SIBLING_DEFAULT = "sibling"


def sibling_word(relationship: int | None) -> str:
    """Resolve a login relationship byte to its English sibling word.

    1/2 -> "brother", 3/4 -> "sister", 0/None/unknown -> "sibling".
    """
    if relationship is None:
        return _SIBLING_DEFAULT
    return _SIBLING_WORD.get(relationship, _SIBLING_DEFAULT)


def protect_tags(text: str, sibling_relationship: int | None = None) -> str:
    """Swap every known variable/color tag in ``text`` to an MT-proof sentinel.

    Mirrors upstream's forward swap (translate.py:323-328): color tags first (``<color_x>`` ->
    ``<&color_x>``), then the fixed placeholder table, then the sibling-relationship tags. The
    sibling tags resolve to the English word for the given ``sibling_relationship`` byte
    (1/2 brother, 3/4 sister, else sibling) behind a sentinel so MT preserves the chosen word.

    Pure: no Translator import, no mutation of module state. Safe to call on text with no tags
    (returns it unchanged).
    """
    # Color tags: <color_red> -> <&color_red>. The & prefix neutralizes the tag for MT; the
    # tolerant restore puts it back. Mirrors translate.py:325.
    text = re.sub(r"<color_(\w+)>", r"<&color_\1>", text)
    # Fixed placeholder table (longest/most-specific tags first — see ordering note on the table).
    for tag, sentinel in _TAG_SENTINELS:
        text = text.replace(tag, sentinel)
    # Sibling-relationship tags: resolve to the English word behind a sentinel so the word that
    # finally renders is correct AND survives MT untouched.
    word = sibling_word(sibling_relationship)
    sentinel_word = f"{_KYODAI_REL_SENTINEL}{word}{_KYODAI_REL_SENTINEL}"
    for tag in _KYODAI_REL_TAGS:
        text = text.replace(tag, sentinel_word)
    return text


def _tolerant_sentinel_pattern(sentinel: str) -> re.Pattern[str]:
    """Build a regex that matches ``sentinel`` even after MT mangles it.

    MT commonly (a) lowercases, (b) inserts stray spaces inside/around the angle brackets and
    underscores, (c) very occasionally strips the leading ``<``, and (d) *changes the length of
    a long repeated-character run* (drops or duplicates an ``a`` in the padding). We turn the
    sentinel's literal characters into a pattern that:
      * matches case-insensitively,
      * tolerates optional whitespace between every pair of adjacent characters,
      * tolerates the leading ``<`` being dropped,
      * matches a repeated-character run of *any* length wherever the sentinel padded with one.
    Upstream's swap_back hard-codes ±1 length variants for every a-run ≥ 6 (translate.py:105-159);
    a ``(?:a\\s*)+`` run is the general form of that and tolerates an arbitrary count drift. The
    sentinels are unique opaque ASCII, so a permissive pattern can't collide with real text, and
    each sentinel's distinct suffix letter (b, c, d, … or none) keeps the patterns unambiguous
    even though the run length is now wild-carded.
    """
    inner = sentinel[1:-1]  # strip the wrapping < >
    # Build the body pattern char-by-char, but collapse any run of 3+ identical characters into a
    # length-agnostic ``(?:char\s*)+`` so an MT-mangled run of any length still matches. (A run of
    # 3 is the threshold: only the deliberate padding runs are that long; the prefix like "&13_" is
    # not.) Whitespace is tolerated between every pair of adjacent characters elsewhere.
    parts: list[str] = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        run = 1
        while i + run < n and inner[i + run] == ch:
            run += 1
        esc = re.escape(ch)
        if run >= 3:
            # Length-agnostic run: one-or-more of this character, each optionally MT-spaced.
            parts.append(rf"(?:{esc}\s*)+")
            i += run
        else:
            for _ in range(run):
                parts.append(esc)
                i += 1
    # Join with optional whitespace between adjacent atoms (mirrors MT sprinkling spaces). A run
    # atom already absorbs trailing whitespace internally, so a plain "\s*" join stays correct.
    inner_pat = r"\s*".join(parts)
    # Two accepted forms, mirroring upstream's color restore (translate.py:446-447):
    #   "< ... >"  -> the leading "<" present (optionally followed by MT spaces), OR
    #   "& ... >"  -> MT ate the leading "<"; the body starts with "&", and a negative lookbehind
    #                 for "<" stops a preceding real space from being swallowed.
    # ``inner`` always begins with "&" (every sentinel is "<&...>"), so the no-bracket branch keys
    # off that "&" — it can't match arbitrary text.
    return re.compile(
        r"(?:<\s*|(?<!<))" + inner_pat + r"\s*>",
        re.IGNORECASE,
    )


# Pre-compile a tolerant pattern for every sentinel once, at import time.
_RESTORE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_tolerant_sentinel_pattern(sentinel), tag) for tag, sentinel in _TAG_SENTINELS
]

# Tolerant color-tag restore. Two patterns mirroring upstream (translate.py:446-447,458-459):
#   1. the normal <&color_x> form (allowing MT-inserted spaces / lowercasing inside),
#   2. the degraded form where MT ate the leading "<" (&color_x>) — here we must NOT eat the
#      preceding character (negative lookbehind for "<"), so a leading "<?\s*" doesn't swallow a
#      real space in front of the tag.
_COLOR_RESTORE = re.compile(r"<\s*&\s*c\s*o\s*l\s*o\s*r\s*_\s*(\w+)\s*>", re.IGNORECASE)
_COLOR_RESTORE_NOBRACKET = re.compile(
    r"(?<!<)&\s*c\s*o\s*l\s*o\s*r\s*_\s*(\w+)\s*>", re.IGNORECASE
)

# Tolerant sibling-word restore: <&7_aa>word<&7_aa> -> word. Captures the word the translator may
# have re-spaced but should NOT have translated (it was wrapped in sentinels).
_KYODAI_HALF = r"(?:<\s*|(?<!<))&\s*7\s*_\s*a\s*a\s*>"
_KYODAI_REL_RESTORE = re.compile(
    _KYODAI_HALF + r"\s*(\w+)\s*" + _KYODAI_HALF,
    re.IGNORECASE,
)
# Defensive: a lone/stray sentinel half (e.g. MT dropped one side) collapses to nothing rather
# than leaking the raw sentinel into the rendered string.
_KYODAI_REL_STRAY = re.compile(_KYODAI_HALF, re.IGNORECASE)


def restore_tags(text: str) -> str:
    """Swap MT-proof sentinels back to their original game tags (typo-tolerant).

    Reverse of :func:`protect_tags`. Tolerates MT lowercasing the sentinel, inserting stray
    spaces inside it, and dropping the leading ``<`` — mirroring upstream's defensive swap-back
    (translate.py:445-459). Color tags and sibling-relationship words are restored too.

    Pure. Safe on text that never went through :func:`protect_tags` (returns it unchanged).
    """
    # Sibling word first: collapse <&7_aa>word<&7_aa> -> word, then mop up any stray half-sentinel.
    text = _KYODAI_REL_RESTORE.sub(r"\1", text)
    text = _KYODAI_REL_STRAY.sub("", text)
    # Fixed placeholder sentinels -> original tags.
    for pattern, tag in _RESTORE_PATTERNS:
        text = pattern.sub(tag, text)
    # Color sentinels -> original color tags (normal form first, then the dropped-"<" form).
    text = _COLOR_RESTORE.sub(r"<color_\1>", text)
    text = _COLOR_RESTORE_NOBRACKET.sub(r"<color_\1>", text)
    return text
