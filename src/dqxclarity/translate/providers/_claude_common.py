"""Shared Claude prompt constants + response parsers.

Both the CLI provider (``claude_cli``) and the HTTP-API provider (``claude_api``) translate the
SAME way and parse the SAME model output — only the transport differs (subprocess vs httpx). The
prompts and parse helpers live here so the two providers can't drift apart. ``claude_cli`` re-exports
these names so existing imports (``from ...claude_cli import _SYSTEM, _SYSTEM_RICH`` and
``ClaudeCliProvider._parse`` / ``._parse_rich`` / ``._extract_array``) keep resolving.
"""

from __future__ import annotations

import json

_SYSTEM = (
    "You are a translation engine for the MMO game Dragon Quest X (ドラゴンクエストX). "
    "Translate each Japanese string in the input JSON array into natural, concise English as it "
    "would appear in-game. Keep any %s/%d-style placeholders intact. Output single-line prose "
    "(the client re-wraps it); do not add your own line breaks. "
    "Return ONLY a JSON array of strings, same length and order as the input, no commentary."
)

_SYSTEM_RICH = (
    "You are the expert English localizer for the MMORPG Dragon Quest X (ドラゴンクエストX),\n"
    "a high-fantasy Dragon Quest title. You produce the FINAL, polished English shown in-game,\n"
    "upgrading a rough machine-translated draft.\n"
    "\n"
    "INPUT: a JSON array of objects. Each object has:\n"
    '  - "ja":       the Japanese source to translate. It may contain opaque ASCII placeholder\n'
    "                tokens shaped like <&NN_xxx> (e.g. <&13_aaaaaaa>, <&7_ab>). These are\n"
    "                non-text control tokens.\n"
    '  - "glossary": an object of {japanese_term: official_English} pins for proper nouns\n'
    '                (people, places, monsters, skills) appearing in "ja". If a term is listed,\n'
    "                you MUST render it with the given official English, spelled exactly.\n"
    '  - "names":    an object of {japanese_name: English_name} for the player/their sibling.\n'
    "                Use these spellings if such a name appears.\n"
    '  - "baseline": a rough machine translation of this line, or null. It is OFTEN WRONG or\n'
    "                awkward. Use it ONLY as a meaning hint; do not copy its phrasing or its\n"
    '                mistakes. Translate the "ja" yourself.\n'
    '  - "surface":  a hint about where the text appears (e.g. "dialogue", "quest", "menu",\n'
    '                "network_text"), or null. Match the register: dialogue is natural spoken\n'
    "                English; menus/quests are concise and noun-like.\n"
    "\n"
    "RULES:\n"
    '  1. Translate every "ja" into natural, concise in-game English in the Dragon Quest house\n'
    "     style (warm, lightly archaic-fantasy, never literal or robotic).\n"
    "  2. Preserve EVERY <&...> placeholder token and every %s/%d-style format specifier EXACTLY\n"
    '     as it appears in "ja" — same characters, same count, same relative order. Never\n'
    "     translate, reorder, space-pad, or drop them. Treat each <&...> token as a single opaque\n"
    "     word that may sit anywhere in your sentence.\n"
    "  3. Apply glossary pins and name spellings consistently.\n"
    "  4. Output SINGLE-LINE prose — no line breaks; the client re-wraps.\n"
    "  5. Do NOT romanize or transliterate placeholder tokens or names you were not given.\n"
    "\n"
    "OUTPUT: ONLY a JSON array of strings, the SAME length and order as the input array. No\n"
    'commentary, no objects, no keys — just ["english 1", "english 2", ...].'
)


def _extract_array(stdout: str) -> str:
    """Unwrap the `--output-format json` envelope + strip a ```json fence; return array text.

    Returns the candidate text the caller then slices/parses for the array (the part the old
    ``_parse`` computed before its final ``json.loads``). Shared by ``_parse`` (string-only) and
    ``_parse_rich`` (object-tolerant) so the envelope/fence handling lives in one place.
    """
    text = stdout.strip()
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"]
    except json.JSONDecodeError:
        pass  # maybe the CLI already gave us the bare result

    text = text.strip()
    # The model may wrap the array in a ```json fence; strip it.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("[") :]
    return text


def _parse(stdout: str, n: int) -> list[str | None]:
    """Extract the model's text from the `--output-format json` envelope, then the array."""
    text = _extract_array(stdout)
    try:
        arr = json.loads(text[text.find("[") : text.rfind("]") + 1])
    except (json.JSONDecodeError, ValueError):
        return [None] * n
    if not isinstance(arr, list) or len(arr) != n:
        return [None] * n
    return [str(x) if x is not None else None for x in arr]


def _parse_rich(stdout: str, n: int) -> list[str | None]:
    """Parse the rich path's result: an array of strings (or, defensively, ``{"en": ...}`` objects).

    Unlike ``_parse``, this must NOT blindly ``str(x)`` each element — a returned ``{"en": ...}``
    object would stringify into ``"{'en': '...'}"`` garbage. So: try ``json.loads`` on the WHOLE
    extracted text first (robust for arrays of objects), falling back to the first-``[``…last-``]``
    slice only on failure. Per element: ``str`` -> itself, ``dict`` with ``"en"`` -> ``str(d["en"])``,
    ``None`` -> None, ANYTHING else -> reject the whole batch (``[None]*n``, today's fail-safe).
    """
    text = _extract_array(stdout)
    try:
        arr = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            arr = json.loads(text[text.find("[") : text.rfind("]") + 1])
        except (json.JSONDecodeError, ValueError):
            return [None] * n
    if not isinstance(arr, list) or len(arr) != n:
        return [None] * n
    out: list[str | None] = []
    for x in arr:
        if x is None:
            out.append(None)
        elif isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and "en" in x:
            out.append(str(x["en"]))
        else:
            return [None] * n
    return out
