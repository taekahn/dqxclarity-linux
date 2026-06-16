"""Free Google Translate provider (no API key, no metered cost).

Uses the public mobile translate endpoint (the same one upstream dqxclarity uses) and parses the
English out of the HTML. Fast (~100-300ms/phrase), so it's suitable for *synchronous* first-view
dialogue translation. It's an unofficial endpoint: it can rate-limit or change, in which case
translate() returns None per item and the caller leaves the text Japanese.
"""

from __future__ import annotations

import html
import re
import time
import urllib.parse

import httpx

# The free endpoint flakes intermittently — it returns a page with NO result-container (or rate-limits
# with an HTTP error) on a request that succeeds moments later. This is especially common on glossified
# mixed Japanese+English input (a glossary term like 駅->"train station" injected into JA confuses it).
# A couple of quick retries turn those transient empties into a hit; a genuinely empty result-container
# is returned as-is (no retry). Kept small so a real outage still fails fast (the caller leaves the text
# Japanese and the surface re-fires on the next view).
_ATTEMPTS = 3
_RETRY_BACKOFF = 0.25

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
        url = f"https://translate.google.com/m?hl=en&sl=ja&tl=en&q={q}"
        for attempt in range(_ATTEMPTS):
            try:
                resp = self._client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                resp = None
            if resp is not None:
                m = _RESULT_RE.search(resp.text)
                if m:  # got the result container -> that's the answer (even if it's empty -> None)
                    return html.unescape(m.group(1).strip()) or None
            # HTTP error or no result container = a transient flake; retry after a short backoff.
            if attempt + 1 < _ATTEMPTS:
                time.sleep(_RETRY_BACKOFF)
        return None
