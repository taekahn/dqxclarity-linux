"""Translation provider that shells out to the headless Claude Code CLI (`claude -p`).

Runs under the user's Claude subscription (no metered API). Designed for the *small, cached*
tail of text not covered by the curated DB: strings are batched into one invocation (amortizing
CLI startup) and every result is cached by the caller, so call volume stays low. See PLAN §10.

Failures (CLI missing, throttled, unparseable) return None per item — the caller then leaves
the text untranslated rather than blocking gameplay.
"""

from __future__ import annotations

import json
import shutil
import subprocess

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


class ClaudeCliProvider:
    name = "claude_cli"

    def __init__(self, *, model: str = "", timeout: float = 120.0) -> None:
        self.model = model
        self.timeout = timeout
        self._bin = shutil.which("claude")

    def available(self) -> bool:
        return self._bin is not None

    def translate(self, texts: list[str]) -> list[str | None]:
        if not texts:
            return []
        if not self._bin:
            return [None] * len(texts)

        prompt = f"{_SYSTEM}\n\nInput:\n{json.dumps(texts, ensure_ascii=False)}"
        cmd = [self._bin, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (subprocess.TimeoutExpired, OSError):
            return [None] * len(texts)
        if proc.returncode != 0:
            return [None] * len(texts)

        return self._parse(proc.stdout, len(texts))

    def translate_rich(self, items: list[dict]) -> list[str | None]:
        """Rich-context batch translate: each item carries ja + glossary/names/baseline/surface.

        Mirrors :meth:`translate`'s guards and subprocess handling, but builds the prompt from the
        rich DQX system prompt (``_SYSTEM_RICH``) over the JSON ``items`` and parses with the
        object-tolerant :meth:`_parse_rich`. Detected by the worker via ``hasattr(bg, "translate_rich")``.
        """
        if not items:
            return []
        if not self._bin:
            return [None] * len(items)

        prompt = f"{_SYSTEM_RICH}\n\nInput:\n{json.dumps(items, ensure_ascii=False)}"
        cmd = [self._bin, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (subprocess.TimeoutExpired, OSError):
            return [None] * len(items)
        if proc.returncode != 0:
            return [None] * len(items)

        return self._parse_rich(proc.stdout, len(items))

    @staticmethod
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

    @staticmethod
    def _parse(stdout: str, n: int) -> list[str | None]:
        """Extract the model's text from the `--output-format json` envelope, then the array."""
        text = ClaudeCliProvider._extract_array(stdout)
        try:
            arr = json.loads(text[text.find("[") : text.rfind("]") + 1])
        except (json.JSONDecodeError, ValueError):
            return [None] * n
        if not isinstance(arr, list) or len(arr) != n:
            return [None] * n
        return [str(x) if x is not None else None for x in arr]

    @staticmethod
    def _parse_rich(stdout: str, n: int) -> list[str | None]:
        """Parse the rich path's result: an array of strings (or, defensively, ``{"en": ...}`` objects).

        Unlike ``_parse``, this must NOT blindly ``str(x)`` each element — a returned ``{"en": ...}``
        object would stringify into ``"{'en': '...'}"`` garbage. So: try ``json.loads`` on the WHOLE
        extracted text first (robust for arrays of objects), falling back to the first-``[``…last-``]``
        slice only on failure. Per element: ``str`` -> itself, ``dict`` with ``"en"`` -> ``str(d["en"])``,
        ``None`` -> None, ANYTHING else -> reject the whole batch (``[None]*n``, today's fail-safe).
        """
        text = ClaudeCliProvider._extract_array(stdout)
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
