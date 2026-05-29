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

    @staticmethod
    def _parse(stdout: str, n: int) -> list[str | None]:
        """Extract the model's text from the `--output-format json` envelope, then the array."""
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
        try:
            arr = json.loads(text[text.find("[") : text.rfind("]") + 1])
        except (json.JSONDecodeError, ValueError):
            return [None] * n
        if not isinstance(arr, list) or len(arr) != n:
            return [None] * n
        return [str(x) if x is not None else None for x in arr]
