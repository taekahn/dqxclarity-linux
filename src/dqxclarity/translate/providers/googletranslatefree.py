"""Free Google Translate provider (no API key, no metered cost).

Uses the public mobile translate endpoint (the same one upstream dqxclarity uses) and parses the
English out of the HTML. Fast (~100-300ms/phrase), so it's suitable for *synchronous* first-view
dialogue translation. It's an unofficial endpoint: it can rate-limit or change, in which case
translate() returns None per item and the caller leaves the text Japanese.
"""

from __future__ import annotations

import html
import re
import urllib.parse

import httpx

_RESULT_RE = re.compile(r'<div class="result-container">(.*?)</div>', re.DOTALL)
_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Mobile Safari/537.36"
)


class GoogleTranslateFreeProvider:
    name = "googletranslatefree"

    def __init__(self, *, timeout: float = 4.0, **_: object) -> None:
        self._client = httpx.Client(headers={"User-Agent": _UA}, timeout=timeout)

    def available(self) -> bool:
        return True

    def translate(self, texts: list[str]) -> list[str | None]:
        out: list[str | None] = []
        for phrase in texts:
            out.append(self._one(phrase))
        return out

    def _one(self, phrase: str) -> str | None:
        if not phrase.strip():
            return None
        q = urllib.parse.quote(phrase)
        try:
            resp = self._client.get(f"https://translate.google.com/m?hl=en&sl=ja&tl=en&q={q}")
            resp.raise_for_status()
        except httpx.HTTPError:
            return None
        m = _RESULT_RE.search(resp.text)
        if not m:
            return None
        return html.unescape(m.group(1).strip()) or None
