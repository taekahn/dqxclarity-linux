"""Tag-preserving dialogue translation.

DQX dialogue is interleaved with control codes the game's text engine needs: ``<close>`` /
``<wait=N>`` terminators, ``<select>``/``<case>``/``<break>`` menus, ``<color_x>``, ``<yesno>``,
``<pc>`` name tags, etc. Dropping them breaks rendering — a missing terminator makes a box show
text and then vanish. So we translate only the Japanese *between* tags and reassemble with the
structural tags intact (the approach upstream uses, validated against the community DB).

``<br>`` page breaks are flattened and re-derived by wrapping (community EN does the same — its
pages are a mechanical 3 lines wide-46), so the English paginates to the box regardless of where
the Japanese broke. ``<select>`` choice menus are handled specially: the dialogue wraps normally
but each option stays on its own unwrapped line, preserving the menu scaffold.
"""

from __future__ import annotations

import re

from .pipeline import Translator
from .wrap import normalize_source, wrap_tagged

_TAG_RE = re.compile(r"(<[^>]*>)")
_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
_SELECT_RE = re.compile(r"<select>(.*?)<select_end>", re.S)


def _is_japanese(text: str) -> bool:
    return bool(_JA_RE.search(text))


def _xlate(
    translator: Translator, ja_text: str, sync: bool, surface: str | None = None
) -> tuple[str | None, bool]:
    """Translate one text fragment. Returns (english_or_None, pending).

    ``sync`` translates inline (first-view, for fast providers on the blocking hook); otherwise it
    queues for background MT and reports pending. ``surface`` is the optional register hint threaded
    from the dispatch closure; it rides the enqueue so the rich Claude provider can match register.
    """
    norm = normalize_source(ja_text)
    if not norm:
        return "", False
    if not _is_japanese(norm):
        return norm, False
    en = translator.lookup(norm)
    if en is not None and en != norm:
        # Cache hit. Still queue a quality upgrade: this fragment may be cached at a LOWER quality
        # (e.g. googletranslatefree) than the configured background provider (e.g. claude_cli), and
        # the dialogue path reaches the cache via lookup() directly — it never goes through
        # translate_now(), which is the only other place that requests an upgrade on a hit. Without
        # this, an already-cached dialogue line is NEVER upgraded on re-view (the whole point of the
        # two-tier design), so dialogue silently stays at first-view quality forever. request_upgrade
        # is a no-op when no strictly-better provider exists or the line is already in flight.
        translator.request_upgrade(norm, surface=surface)
        return en, False
    if sync:
        en = translator.translate_now(norm, surface=surface)
        return (en, False) if en else (None, True)
    translator.request(norm, surface=surface)
    return None, True


def _translate_body(
    translator: Translator, ja: str, width: int, lpp: int, sync: bool, surface: str | None = None
) -> tuple[str | None, bool]:
    """Translate a dialogue body, preserving tags and wrapping. Returns (text_or_None, pending)."""
    flattened = ja.replace("<br>", " ")
    items: list[tuple[str, str]] = []  # (kind, value)
    pending = False
    for part in _TAG_RE.split(flattened):
        if not part:
            continue
        if _TAG_RE.fullmatch(part):
            items.append(("tag", part))
            continue
        en, p = _xlate(translator, part, sync, surface)
        if p:
            pending = True
        elif en:
            items.append(("text", en))
    if pending:
        return None, True
    if not any(k == "text" for k, _ in items):
        # No translatable text — pass the control tags through unchanged.
        return "".join(v for k, v in items if k == "tag"), False
    # Interleave the translated text runs and the literal tags in original order, never dropping a
    # tag (keeping every tag in order also keeps color open/close balanced). A single space joins
    # two adjacent text runs; tags glue to the following text with no extra surrounding space.
    # wrap_tagged then wraps the visible text, treating tags as zero-width/unbreakable, so a leading
    # tag stays glued to the first word and trailing tags (e.g. <yesno><close>) ride the last line.
    parts: list[str] = []
    for k, v in items:
        if k == "tag":
            parts.append(v)
        else:  # text: separate from a preceding text run with a space
            if parts and not _TAG_RE.fullmatch(parts[-1]):
                parts.append(" ")
            parts.append(v)
    interleaved = re.sub(r" +", " ", "".join(parts))
    return wrap_tagged(interleaved, width, lpp), False


def translate_conversation(
    translator: Translator, ja: str, width: int = 46, lines_per_page: int = 3, sync: bool = False,
    surface: str | None = None,
) -> str | None:
    """Translate dialogue, preserving control tags and menu structure.

    With ``sync`` (fast provider on the blocking hook), missing fragments are translated inline so
    the line renders in English on first view. Otherwise missing fragments are queued for
    background MT and the function returns None until everything is cached. ``surface`` is the
    optional register hint threaded from the dispatch closure down to each background enqueue.
    """
    sel = _SELECT_RE.search(ja)
    if not sel:
        out, pending = _translate_body(translator, ja, width, lines_per_page, sync, surface)
        if pending:
            return None
        # No-pagination profile (e.g. the quest menu, which renders <br> literally): join the
        # segments with plain newlines and drop any <br> page-break markers entirely. With
        # lines_per_page < 1 wrap_dialogue already emits no <br>; this is a belt-and-braces strip
        # so a community-cached string that *contains* <br> doesn't render a literal tag.
        if lines_per_page < 1 and out:
            out = out.replace("\n<br>\n", "\n").replace("<br>", "\n")
        return out

    # Choice menu: translate the lead-in dialogue normally, each option on its own line, and keep
    # the <select>/<select_end>/<case>/<break>/<case_end> scaffold intact.
    dialogue, pending = _translate_body(
        translator, ja[: sel.start()], width, lines_per_page, sync, surface
    )
    if pending:
        return None
    options: list[str] = []
    for opt in sel.group(1).split("\n"):
        opt = opt.strip()
        if not opt:
            continue
        en, p = _xlate(translator, opt, sync, surface)
        if p:
            return None  # wait until every option is translated
        if en:
            options.append(en)
    scaffold = ja[sel.end() :]  # trailing <case>/<break>/<case_end> tags, kept as-is
    menu = f"<select>\n{chr(10).join(options)}\n<select_end>{scaffold}"
    return f"{dialogue}\n{menu}" if dialogue else menu
