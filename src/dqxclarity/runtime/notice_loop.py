"""Live translation of the startup "Important Notice" announcement body (backlog #27).

Unlike dialogue/quest/network_text — which flow through a CODE HOOK — the Important Notice body
does NOT pass through any hookable function. A 15-minute full ``network_text`` capture never sees
it, and it never appears in the live ``network_text`` log. ROOT CAUSE (confirmed): the notice is a
STATIC, null-terminated UTF-8 buffer that already sits in game memory by the time the login screen
renders. So — exactly like the concierge/party/chat NAME scanner (``names_loop``) — it must be
handled by a memory SCANNER: find the buffer, read the JA, translate, write the EN back in place.

This module mirrors ``names_loop``'s scanner shape (find → read → translate → re-read-guard → write
within a byte budget) but the PROSE handling is deliberately kept separate from the name romaji
logic, because the notice carries structure the names never do:

  * a formatting PREFIX of control bytes (``@D\\xd8\\x02``) before the first readable char, which the
    game expects to stay verbatim (analogous to the names' ``\\x04`` write_prefix);
  * embedded ``\\n`` line breaks and a LITERAL ``<PAGE>`` page-break token on its own line;
  * ``https://…`` URLs that must survive untranslated.

The translate pipeline (one direction, top to bottom):

    prefix split  →  page split on <PAGE>  →  per page: URL shield → MT (build_translate_fn)
                  →  URL restore  →  rejoin pages with the exact original <PAGE> delimiter
                  →  re-prepend the prefix  →  budget-checked write_cstring

Idempotency: the scan only acts when the read body is still JAPANESE. Once translated (or once a
buffer reads as already-English on a later launch — the normal pipeline caches the result so the
second launch is instant), the body is non-Japanese and the loop leaves it untouched. So it
translates ONCE per appearance and never churns the buffer every tick.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from ..process.memory_linux import LinuxProcessMemory
from ..process.signatures import NOTICE_STRING

# The notice box is WIDE (similar to the dialogue box), so wrap at the dialogue default of 46 cols.
# We deliberately do NOT paginate (no <br>): the notice already carries its OWN page breaks via the
# literal <PAGE> token, and the game renders that token — inserting our own <br> pages on top would
# fight the game's existing paging. So this wrap only prevents long EN lines from clipping the box.
NOTICE_WRAP_WIDTH = 46

# The literal page-break token the game embeds in the notice body. It sits on its OWN line
# (``…\n<PAGE>\n…``); we split the body on it, translate each page independently, then rejoin with
# the EXACT original delimiter so the game's paging is preserved byte-for-byte.
PAGE_TOKEN = "<PAGE>"

# URLs must survive machine translation verbatim (MT would mangle/translate-around them). We shield
# every http/https URL behind an opaque sentinel before MT and restore it after. We verified
# translate/placeholders.py only swaps player/sibling NAMES — it does NOT shield URLs — so we do it
# here. The sentinel is ASCII-only and contains no Japanese/letters an MT engine would touch, and
# uses a private-use-ish marker unlikely to occur in real text.
_URL_RE = re.compile(r"https?://\S+")
# A zero-width-ish, MT-inert sentinel. ``\x00`` can't appear (cstring terminator) so it's a safe,
# never-in-the-source marker; the index keeps multiple URLs distinct and ordered.
_URL_SENTINEL = "\x01URL{}\x01"


def _is_japanese(text: str) -> bool:
    # Same CJK ranges names_loop uses: hiragana/katakana, CJK ideographs, and the fullwidth/halfwidth
    # block. Used as the idempotency gate — a body that is no longer Japanese was already translated.
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "＀" <= c <= "￯" for c in text)


# The first readable Japanese char in the notice is the opening bracket 「 (UTF-8 E3 80 8C). The live
# buffer opens with a formatting prefix of control bytes (``@D\xd8\x02`` = 40 44 D8 02) BEFORE it.
# Those bytes are NOT valid standalone UTF-8 (\xd8 is a lead byte for a 2-byte sequence that \x02 does
# not complete), so we MUST handle the prefix at the BYTE level — decoding the whole buffer would lose
# \xd8 to a replacement char and make the round-trip lossy. We split the prefix off as raw bytes, keep
# them verbatim, and decode/translate only the valid-UTF-8 text after them.
_FIRST_TEXT_BRACKET = "「".encode()  # E3 80 8C — the notice always opens its readable text with this


def _split_prefix_bytes(raw: bytes) -> tuple[bytes, str]:
    """Split the leading formatting control bytes off the readable UTF-8 text.

    Returns ``(prefix_bytes, text)`` where ``prefix_bytes`` is the verbatim control prefix to
    re-prepend on write (analogous to the names' ``write_prefix``) and ``text`` is the decoded,
    translatable remainder. We locate the first readable char by finding the opening 「 bracket the
    notice always begins with; everything before it is the prefix. We do NOT hardcode the prefix
    length (it can vary) — only that the readable body starts at 「. If 「 isn't found (an
    unexpected notice shape, or an already-translated EN buffer with no JA bracket) we treat the
    whole buffer as text with no prefix, so a non-notice/already-EN read is still handled safely.
    """
    idx = raw.find(_FIRST_TEXT_BRACKET)
    if idx == -1:
        return b"", raw.decode("utf-8", "replace")
    return raw[:idx], raw[idx:].decode("utf-8", "replace")


def _shield_urls(text: str) -> tuple[str, list[str]]:
    """Replace each URL with an MT-inert sentinel, returning (shielded_text, urls_in_order)."""
    urls: list[str] = []

    def repl(m: re.Match) -> str:
        urls.append(m.group(0))
        return _URL_SENTINEL.format(len(urls) - 1)

    return _URL_RE.sub(repl, text), urls


def _restore_urls(text: str, urls: list[str]) -> str:
    """Put the original URLs back where their sentinels are."""
    for i, url in enumerate(urls):
        text = text.replace(_URL_SENTINEL.format(i), url)
    return text


def translate_body(raw: bytes, translate_fn) -> bytes | None:
    """Translate a full notice buffer, preserving prefix / pages / URLs. Returns None if unchanged.

    Takes the RAW null-terminated bytes (no trailing NUL) and returns the RAW translated bytes
    (prefix + EN, no NUL) — raw because the formatting prefix isn't valid standalone UTF-8 and must
    survive byte-for-byte (see ``_split_prefix_bytes``).

    ``translate_fn`` is a prose ``fn(ja) -> str | None`` built ONCE from
    ``dispatch.build_translate_fn`` (community-first, then MT, placeholder-safe, wrapped). We reuse
    it so the notice rides the SAME pipeline as dialogue — no hand-rolled MT.

    Pipeline (see module docstring): split off the control prefix (bytes); split the readable text
    into pages on the literal ``<PAGE>`` token; per page shield URLs → translate → restore URLs;
    rejoin with the exact original ``<PAGE>`` delimiter; re-prepend the prefix bytes; encode.
    """
    prefix, text = _split_prefix_bytes(raw)

    # Preserve the page structure EXACTLY: split on the literal token and translate each page on its
    # own. A page that doesn't translate (None / non-Japanese / unchanged) keeps its original text,
    # so a partially-English notice still round-trips.
    pages = text.split(PAGE_TOKEN)
    out_pages: list[str] = []
    changed = False
    for page in pages:
        shielded, urls = _shield_urls(page)
        en = translate_fn(shielded)
        if en is None or en == shielded:
            out_pages.append(page)  # no translation -> keep this page verbatim
            continue
        out_pages.append(_restore_urls(en, urls))
        changed = True

    if not changed:
        return None
    # Rejoin with the EXACT original delimiter, re-prepend the verbatim control prefix, encode to
    # bytes for the in-place write. errors="replace" can't lose anything here (EN + the JA we kept
    # are valid UTF-8); the prefix is already bytes so it round-trips exactly.
    return prefix + PAGE_TOKEN.join(out_pages).encode("utf-8", "replace")


def find_notice(mem: LinuxProcessMemory) -> int | None:
    """Locate the START address of the notice buffer, or None if the anchor isn't present.

    NOTICE_STRING matches MID-buffer (the streaming-disclaimer phrase 動画配信の際はサーバー). The
    full null-terminated string starts EARLIER, so we scan BACKWARD from the anchor to the previous
    NUL byte; the string begins at the byte AFTER that NUL. We never hardcode the 338-byte back-step
    or the buffer length — both vary with the live notice — so we read a window before the anchor and
    find the NUL.
    """
    anchor = mem.pattern_scan(NOTICE_STRING, return_multiple=False)
    if anchor is None:  # pattern_scan returns the address or None; address 0 is never mapped anyway
        return None

    # Read a generous window ending at the anchor and find the LAST NUL before it. The notice's
    # start is the byte just after that NUL. 2048 is comfortably larger than any observed back-step.
    window = 2048
    read_start = max(0, anchor - window)
    pre = mem.read(read_start, anchor - read_start)
    nul = pre.rfind(b"\x00")
    if nul == -1:
        # No NUL in the window — treat the window start as the string start (defensive; shouldn't
        # happen for a real notice, whose buffer is preceded by other NUL-terminated data).
        return read_start
    return read_start + nul + 1


def _read_raw_cstring(mem: LinuxProcessMemory, addr: int, max_len: int) -> bytes:
    """Read the RAW null-terminated bytes at ``addr`` (without the terminator).

    We need the raw bytes (not ``read_cstring``'s decoded str) because the formatting prefix isn't
    valid standalone UTF-8 — decoding would corrupt it (see ``_split_prefix_bytes``). We read a
    window and cut at the first NUL.
    """
    raw = mem.read(addr, max_len)
    nul = raw.find(b"\x00")
    return raw if nul == -1 else raw[:nul]


def translate_notice(mem: LinuxProcessMemory, translate_fn, *, max_len: int = 2048) -> bool:
    """One-shot: find the notice, translate it, and write the EN back within budget. Returns written.

    IDEMPOTENT: returns False (writing nothing) when the anchor is absent, the body is no longer
    Japanese (already translated — the idempotency gate), the translation is a no-op, the body
    changed between read and write (re-read guard), or the EN doesn't fit the buffer (budget).

    Budget = the ORIGINAL JA buffer's byte length + the NUL terminator. EN is ~1 byte/char so it
    normally fits, but we ENFORCE it and SKIP the write (leaving the JA) rather than ever overflow
    the game's fixed buffer. We work in RAW BYTES throughout so the non-UTF-8 prefix survives intact.
    """
    start = find_notice(mem)
    if start is None:
        return False

    ja_raw = _read_raw_cstring(mem, start, max_len)
    if not ja_raw or not _is_japanese(ja_raw.decode("utf-8", "replace")):
        # Idempotency gate: an already-English (or empty) buffer is left untouched — we do NOT
        # re-translate every tick. This is also what makes a cached second launch a no-op. (The
        # decode is for the language test only; the prefix bytes lost to "replace" here don't matter
        # because if the body is Japanese we re-split the RAW bytes in translate_body.)
        return False

    en_raw = translate_body(ja_raw, translate_fn)
    if not en_raw or en_raw == ja_raw:
        return False

    # Re-read guard against the buffer changing between scan and write (same pattern as names_loop).
    if _read_raw_cstring(mem, start, max_len) != ja_raw:
        return False

    # Budget = the JA byte span + its NUL. If EN + NUL exceeds it, SKIP the write entirely (never
    # truncate into overflow). On a write we zero-pad to the full budget so no stale JA tail bytes
    # remain after a SHORTER EN replacement — same guarantee write_cstring gives the names path.
    budget = len(ja_raw) + 1
    payload = en_raw + b"\x00"
    if len(payload) > budget:
        return False
    payload += b"\x00" * (budget - len(payload))
    mem.write(start, payload)
    return True


def run(
    mem: LinuxProcessMemory,
    translate_fn,
    *,
    stop: threading.Event,
    interval: float = 1.0,
    on_write=None,
) -> None:
    """Run until ``stop`` is set: each tick try to translate the notice (idempotent — see above).

    This is meant to share the per-attach scanner thread/lifecycle with the name scanner. It is
    cheap: ``translate_notice`` short-circuits the moment the buffer reads non-Japanese (already
    translated) or the anchor is absent, so a steady-state session does almost no work per tick.
    ``on_write()`` (no args) fires once when a write actually lands, for console feedback.
    """
    while not stop.is_set():
        try:
            wrote = translate_notice(mem, translate_fn)
        except (OSError, ValueError):
            # A transient bad read against a live game (or a partial read) must not kill the loop.
            wrote = False
        if wrote and on_write:
            on_write()
        stop.wait(interval)


@dataclass
class NoticeHandle:
    """Live handle to the per-attach notice-scanner thread (one per game attach).

    Mirrors ``names_loop.ScannerHandle``: ``start_notice_scanner`` starts one inside each attach's
    ``hook_session`` block with its OWN private stop Event (NOT the supervisor's shared ``stop``, for
    the same game-gone reasoning as the name scanner), and ``stop_and_join`` winds it down before the
    next re-attach builds a fresh ``mem``. When disabled the handle carries ``thread=None`` so the
    call site's ``stop_and_join`` is an unconditional no-op (uniform call site, no None-guard).
    """

    stop: threading.Event
    thread: threading.Thread | None = None

    def stop_and_join(self, timeout: float | None = None) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)


def start_notice_scanner(
    mem: LinuxProcessMemory,
    translate_fn,
    *,
    enabled: bool,
    interval: float = 1.0,
    on_write=None,
) -> NoticeHandle:
    """Start the notice scanner as a DAEMON thread for ONE game attach, return a handle.

    Same per-attach, private-stop lifecycle as ``names_loop.start_scanner`` (see its docstring for
    why the scanner must NOT key off the supervisor's shared ``stop``). ``translate_fn`` is the prose
    fn built ONCE from ``dispatch.build_translate_fn`` and reused for the life of the attach.

    When ``enabled`` is False no thread is started; the returned handle's ``stop_and_join`` is a
    no-op so the call site stays uniform.
    """
    stop = threading.Event()
    if not enabled:
        return NoticeHandle(stop=stop, thread=None)
    thread = threading.Thread(
        target=run,
        args=(mem, translate_fn),
        kwargs={"stop": stop, "interval": interval, "on_write": on_write},
        name="notice-scanner",
        daemon=True,  # never block process exit on it; run() always stop+joins it explicitly anyway
    )
    thread.start()
    return NoticeHandle(stop=stop, thread=thread)
