"""Format translated dialogue to fit DQX's text box.

DQX dialogue uses ``\\n`` for line breaks and ``<br>`` as a page break ("wait for input, show
next page"). The box shows ~3 lines per page. English of a different length than the Japanese
must be re-wrapped to the box width and re-paginated, or it overflows and gets cut off.

Ported from upstream `common/translate.py` (`__wrap_text` + `__add_line_endings`).
"""

from __future__ import annotations

import re
import textwrap

WRAP_WIDTH = 46
LINES_PER_PAGE = 3


# Characters that render poorly / break the dialog box in English (upstream strips these).
_ODDITIES = ("「", "」", "『", "』", "～", "♪")


def normalize_source(text: str) -> str:
    """Flatten the game's line/page breaks and strip characters that mangle the EN box.

    Notably removes the Japanese quote brackets (「」): Claude echoes them, and a line starting
    with one is dropped by the game's text engine (the "first sentence disappears" symptom).
    """
    flat = text.replace("<br>", " ").replace("\n", " ").replace("　", " ")
    for odd in _ODDITIES:
        flat = flat.replace(odd, "")
    return re.sub(r"\s+", " ", flat).strip()


def add_page_breaks(text: str, lines_per_page: int = LINES_PER_PAGE) -> str:
    """Insert ``<br>`` after every ``lines_per_page`` lines (page breaks for the dialog box).

    ``lines_per_page < 1`` disables pagination (returns the wrapped text unchanged): surfaces
    like the quest menu render ``<br>`` literally instead of paginating on it. The guard also
    avoids a modulo-by-zero on ``lines_per_page == 0``.
    """
    if lines_per_page < 1:
        return text
    lines = [ln for ln in text.split("\n") if ln]
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        if (i + 1) % lines_per_page == 0 and i != len(lines) - 1:
            out.append("<br>")
    return "\n".join(out)


def wrap_dialogue(
    text: str, width: int = WRAP_WIDTH, lines_per_page: int = LINES_PER_PAGE
) -> str:
    """Wrap to ``width`` chars/line and paginate with ``<br>`` for the dialogue box."""
    for odd in _ODDITIES:  # defensive: catch any the translator echoed
        text = text.replace(odd, "")
    wrapped = textwrap.fill(text.strip(), width=width, replace_whitespace=False)
    return add_page_breaks(wrapped, lines_per_page)


_TAG_TOKEN_RE = re.compile(r"<[^>]*>")


def _visible_len(token: str) -> int:
    """Length of a token's visible (non-tag) characters; tags count as zero width."""
    return len(_TAG_TOKEN_RE.sub("", token))


_TAG_PREFIX_RE = re.compile(r"^((?:<[^>]*>)*)(.*)$", re.S)


def _hard_break(token: str, width: int) -> list[str]:
    """Break one oversized token (leading tags + a single long word) into <=width visible chunks.

    A token is at most a run of leading control tags glued to one space-free word, so its visible
    length is the word length. When that exceeds ``width`` (a long romanized name, a URL fragment,
    a compound) the word is sliced every ``width`` chars so no rendered line overflows the box —
    the safety ``textwrap.fill`` gives ``wrap_dialogue`` but a naive greedy fill lacks. The leading
    tags ride on the first chunk so the open tag still travels with its text.
    """
    prefix, word = _TAG_PREFIX_RE.match(token).groups()
    if len(word) <= width:
        return [token]
    chunks = [word[i : i + width] for i in range(0, len(word), width)]
    chunks[0] = prefix + chunks[0]
    return chunks


def wrap_tagged(
    text: str, width: int = WRAP_WIDTH, lines_per_page: int = LINES_PER_PAGE
) -> str:
    """Wrap visible text to ``width``, treating ``<...>`` tags as zero-width and unbreakable.

    Tags never count toward line width and are never split across a line; only the visible words
    wrap. A tag glues to the word that follows it (so a control tag never lands alone at the end of
    a line and an open/close tag travels with its text). Then paginate with
    ``add_page_breaks(.., lines_per_page)`` (``lines_per_page < 1`` => no ``<br>``, per the quest
    profile).
    """
    for odd in _ODDITIES:  # defensive: catch any the translator echoed
        text = text.replace(odd, "")
    text = text.strip()
    if not text:
        return add_page_breaks("", lines_per_page)

    # Tokenize into tags and whitespace-separated words, keeping a tag glued to the following word
    # so "<color_yellow>Your" stays one unbreakable token (the tag never lands alone at line end).
    # Trailing tags with no following word (e.g. "<yesno><close>") form their own zero-width token,
    # which then glues onto the end of the last line below.
    tokens: list[str] = []
    pending_tag = ""  # tags seen since the last word, waiting to glue onto the next word
    pos = 0
    for m in _TAG_TOKEN_RE.finditer(text):
        between = text[pos : m.start()]
        pos = m.end()
        words = between.split()
        for i, w in enumerate(words):
            if i == 0 and pending_tag:
                tokens.append(pending_tag + w)
                pending_tag = ""
            else:
                tokens.append(w)
        pending_tag += m.group(0)
    tail = text[pos:]
    tail_words = tail.split()
    for i, w in enumerate(tail_words):
        if i == 0 and pending_tag:
            tokens.append(pending_tag + w)
            pending_tag = ""
        else:
            tokens.append(w)
    # Any trailing tags with no following word become a final zero-width token.
    if pending_tag:
        tokens.append(pending_tag)

    if not tokens:
        return add_page_breaks("", lines_per_page)

    # Greedy fill: a line's "visible length" counts only non-tag characters. A trailing zero-width
    # tag token (e.g. "<yesno><close>") always glues onto the current line (it adds no width).
    lines: list[str] = []
    cur = ""
    cur_vis = 0
    for tok in tokens:
        vis = _visible_len(tok)
        if vis > width:
            # Oversized single word: hard-break so no line overflows the box (I1). Flush the
            # current line, emit the full-width chunks as their own lines, and carry the remainder
            # forward so following tokens still pack onto it.
            if cur:
                lines.append(cur)
            chunks = _hard_break(tok, width)
            lines.extend(chunks[:-1])
            cur = chunks[-1]
            cur_vis = _visible_len(cur)
            continue
        if not cur:
            cur = tok
            cur_vis = vis
            continue
        # joining space (1) counts toward width only when the token has visible chars
        added = (1 + vis) if vis else 0
        if cur_vis + added <= width:
            cur = f"{cur} {tok}" if vis else f"{cur}{tok}"
            cur_vis += added
        else:
            lines.append(cur)
            cur = tok
            cur_vis = vis
    if cur:
        lines.append(cur)

    return add_page_breaks("\n".join(lines), lines_per_page)
