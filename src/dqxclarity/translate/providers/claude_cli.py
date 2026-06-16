"""Translation provider that shells out to the headless Claude Code CLI (`claude -p`).

Runs under the user's Claude subscription (no metered API). Designed for the *small, cached*
tail of text not covered by the curated DB: strings are batched into one invocation (amortizing
CLI startup) and every result is cached by the caller, so call volume stays low. See PLAN §10.

Failures (CLI missing, throttled, unparseable) return None per item — the caller then leaves
the text untranslated rather than blocking gameplay.

The prompts (``_SYSTEM``/``_SYSTEM_RICH``) and the response parsers (``_extract_array``/``_parse``/
``_parse_rich``) live in :mod:`._claude_common` so the API provider (``claude_api``) shares them and
can't drift. They're re-exported here (module constants + static methods) so existing imports
(``from ...claude_cli import _SYSTEM`` and ``ClaudeCliProvider._parse(...)``) keep resolving.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from ._claude_common import (
    _SYSTEM,
    _SYSTEM_RICH,
    _extract_array,
    _parse,
    _parse_rich,
)

# Re-export so ``from dqxclarity.translate.providers.claude_cli import _SYSTEM, _SYSTEM_RICH`` works.
__all__ = ["ClaudeCliProvider", "_SYSTEM", "_SYSTEM_RICH"]


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

    # The envelope/array parsers are shared with claude_api; exposed as static methods so existing
    # tests (``ClaudeCliProvider._extract_array``/``._parse``/``._parse_rich``) keep resolving.
    _extract_array = staticmethod(_extract_array)
    _parse = staticmethod(_parse)
    _parse_rich = staticmethod(_parse_rich)
