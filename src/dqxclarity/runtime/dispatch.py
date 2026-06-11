"""Shared translation dispatch + the multi-hook serve loop.

`build_translate_fn` produces the per-string resolver used by every hook: community/cached human
translation first (with player-name placeholder swapping), then machine translation. `serve` polls
all installed blocking hooks in one loop.
"""

from __future__ import annotations

import re
import struct
import threading

from ..translate import romanize
from ..translate.dialogue import translate_conversation
from ..translate.placeholders import KYODAI, PC, PN, SN, from_placeholders, to_placeholders

# Player/sibling placeholder conventions, tried in order on a community lookup: dialogue corpus
# (<pnplacehold>/<snplacehold>) first, then quest/event/system corpus (<pc>/<kyodai>).
CONVENTIONS = ((PN, SN), (PC, KYODAI))

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")

# A maximal RUN of Japanese name characters: hiragana (ぁ-ん), katakana (ァ-ヴ plus the prolonged-
# sound mark ー and iteration marks ヽ ヾ 々), and kanji (一-鿿). Used by _translate_name_runs to
# carve a battle-message template into name runs vs. everything-else (markers/ASCII/digits/spaces),
# so only the proper-noun runs are name-ified and the structure around them is preserved verbatim.
_JA_RUN_RE = re.compile(r"[ぁ-んァ-ヺー一-鿿ゝゞヽヾ々]+")


def is_japanese(text: str) -> bool:
    return bool(_JA_RE.search(text))


def _translate_name_runs(text: str, translator) -> str | None:
    """NOVEL (no upstream equivalent): name-ify each Japanese run in a battle message in place.

    The network_text battle surface hands us full templates whose captured argument is a Japanese
    monster/actor name, e.g. ``\\sしびれくらげ\\mしびれくらげ\\e takes <%dB_VALUE> damage!`` (the
    ``\\s`` ``\\m`` ``\\e`` markers wrap the name + an internal id). We split ``text`` into alternating
    NON-Japanese and Japanese runs: every non-Japanese stretch (the markers, ASCII words, spaces,
    digits, punctuation) is preserved VERBATIM and in order; every Japanese run is resolved to a name.

    Name resolution does PLAYER/SIBLING SUBSTITUTION FIRST — this is the headline correctness case:
    the player ``タイカン`` ("Taikan") collides with a cached monster ``タイカン`` ("Squid"), so an
    exact match on the live player/sibling JA name must win over the cache lookup. Otherwise the run
    goes through ``translator.translate_name`` (community/cache hit, else offline romaji).

    NO machine translation / no provider call anywhere here — names are instant (cache or local
    romaji), so this is safe on the combat hot path with zero lag. The ``\\m…\\e`` internal-id portion
    is name-ified too, pending live verification. Returns the rebuilt string if it CHANGED, else None.
    """
    pja, pen = translator.player_name_ja, translator.player_name_en
    sja, sen = translator.sibling_name_ja, translator.sibling_name_en

    def resolve(run: str) -> str:
        if pja and run == pja:
            return pen or translator.translate_name(run)
        if sja and run == sja:
            return sen or translator.translate_name(run)
        return translator.translate_name(run)

    out = _JA_RUN_RE.sub(lambda m: resolve(m.group(0)), text)
    return out if out != text else None


def _make_community_lookup(cfg, translator):
    """Return the whole-string community/cached lookup used by every surface.

    Looks the string up with the player/sibling names swapped to placeholders (so a curated line
    that uses the placeholder matches), and swaps the EN names back in on a hit. Both placeholder
    conventions are tried in order (dialogue corpus, then quest/event/system corpus); the same
    convention that matched is used to swap the EN names back in. Returns None on a miss (or a no-op
    hit that equals the placeholdered input). Shared by the text and name paths.

    The player/sibling names are read from the TRANSLATOR on every call (not captured at build time),
    so the PLAYER hook's apply_names can update them at runtime and the very next lookup uses the new
    names — name auto-detection applies WITHOUT a restart. As a convenience for callers that build a
    Translator directly (without going through the CLI's _build_translator), any names present in
    ``cfg`` seed the translator here when its own names are still empty — so a cfg-only caller keeps
    working while runtime updates remain live.
    """
    # Seed the translator's live names from cfg if they aren't set yet (idempotent: an already-set
    # name — e.g. one the player hook detected — is never overwritten by a stale/empty cfg value).
    for attr in ("player_name_ja", "player_name_en", "sibling_name_ja", "sibling_name_en"):
        if not getattr(translator, attr, "") and getattr(cfg.translate, attr, ""):
            setattr(translator, attr, getattr(cfg.translate, attr))

    def community_lookup(ja: str) -> str | None:
        # Read LIVE from the translator each call — a player-hook update is picked up immediately.
        pja, pen = translator.player_name_ja, translator.player_name_en
        sja, sen = translator.sibling_name_ja, translator.sibling_name_en
        seen: set[str] = set()
        for pn, sn in CONVENTIONS:
            key = to_placeholders(ja, pja, sja, pn=pn, sn=sn)
            if key in seen:
                # No name present (key == ja for every convention) -> same lookup; skip the dupe.
                continue
            seen.add(key)
            en = translator.lookup(key)
            if en and en != key:
                return from_placeholders(en, pen, sen, pn=pn, sn=sn)
        return None

    return community_lookup


def build_translate_fn(
    cfg, translator, *, wrap_width=None, lines_per_page=None, sync=None, suppression=None
):
    """Return (translate_fn, community_lookup) for the given config + translator.

    The keyword overrides let a caller supply a per-surface format profile (from a HookSpec).
    Each falls back to the config default (or, for ``sync``, the presence of a fast sync provider)
    when left ``None``, so existing callers keep the dialogue behaviour unchanged.

    ``suppression`` is an optional ``translate.suppression.SuppressionIndex``. When supplied, a
    BAD STRING pre-pass runs FIRST — BEFORE the community lookup and MT — exactly like upstream's
    dialogue pipeline (search_bad_strings before the cache/MT, dialogue.py:39-47): if the index
    SUBSTRING-matches the incoming ja, its curated EN fallback is returned immediately (with the live
    player/sibling name substituted). Leaving it None preserves the previous behaviour.
    """
    width = cfg.translate.wrap_width if wrap_width is None else wrap_width
    lpp = cfg.translate.lines_per_page if lines_per_page is None else lines_per_page
    fast = (translator.sync_provider is not None) if sync is None else sync

    community_lookup = _make_community_lookup(cfg, translator)

    def translate_fn(ja: str) -> str | None:
        if not is_japanese(ja):
            return None
        if suppression is not None:
            # BAD STRING pre-pass: a substring match returns the curated EN fallback BEFORE the
            # cache/MT (upstream search_bad_strings ordering). Names are read LIVE from the translator
            # so a player-hook update applies without a restart, mirroring community_lookup.
            sup = suppression.match(
                ja,
                player_ja=translator.player_name_ja,
                player_en=translator.player_name_en,
                sibling_ja=translator.sibling_name_ja,
                sibling_en=translator.sibling_name_en,
            )
            if sup:
                return sup
        hit = community_lookup(ja)
        if hit:
            # Human-curated, already game-formatted — but a no-pagination surface (quest menu,
            # lpp<1) renders <br> literally, and community quest/event strings carry <br>. Strip
            # it here so the community path matches the MT path's no-<br> guarantee.
            if lpp < 1:
                hit = hit.replace("\n<br>\n", "\n").replace("<br>", "\n")
            return hit
        return translate_conversation(translator, ja, width, lpp, sync=fast)

    return translate_fn, community_lookup


def build_rewards_translate_fn(items_dict):
    """Return ``fn(ja) -> str | None`` that cleans a STRUCTURED quest-reward field per line.

    Wraps ``translate.rewards.clean_quest_rewards`` with the supplied JA item-name -> EN item-name
    dict (built by ``community.build_reward_items_dict``). Non-Japanese input passes through as None
    (leave it as-is); a cleaned result equal to the input also returns None so the write-back path
    treats it as a no-op (the serve loop only writes when ``en != ja``). The reward fields are a list,
    not prose, so this NEVER calls MT — it only re-formats item names that resolve in the dict and
    leaves the rest as upstream's clean_up_and_return_items does (no crash on an unknown item).
    """
    from ..translate.rewards import clean_quest_rewards

    def translate_fn(ja: str) -> str | None:
        if not is_japanese(ja):
            return None
        cleaned = clean_quest_rewards(ja, items_dict)
        return cleaned if cleaned and cleaned != ja else None

    return translate_fn


class FieldRouter:
    """Per-field translate dispatcher for a multi-field hook (e.g. the quest reward fields).

    ``BlockingHook.serve_once`` duck-types this (it looks for ``fn_for``): when the per-hook
    "translate fn" is a ``FieldRouter`` instead of a plain callable, the hook calls ``fn_for(index)``
    for each field index and applies the returned fn to that field. This lets the quest hook route
    its STRUCTURED reward fields (offsets 640/744, indices 3/4) through the reward-cleanup fn while
    the prose fields (name/description) keep the normal whole-string translate fn — without changing
    the ``(name, hook, fn)`` serve contract or affecting any other hook.

    ``default_fn`` handles every field not in ``overrides``; ``overrides`` maps a field index to its
    specialized fn. A field whose index is absent from ``overrides`` uses ``default_fn``.
    """

    def __init__(self, default_fn, overrides: dict[int, object]):
        self.default_fn = default_fn
        self.overrides = dict(overrides)

    def fn_for(self, index: int):
        return self.overrides.get(index, self.default_fn)

    # Keep it callable so a caller that ignores per-field routing (or a single-field hook) still works
    # exactly like the default fn — defensive, never relied on by the serve loop's router path.
    def __call__(self, ja: str):
        return self.default_fn(ja)


def build_quest_translate_fn(
    cfg, translator, *, reward_field_indices, items_dict,
    wrap_width=None, lines_per_page=None, sync=None, suppression=None,
):
    """Return a ``FieldRouter`` for the quest hook: prose fn for most fields, reward fn for the rewards.

    The default fn is the normal whole-string translate fn (the quest format profile); the reward
    field indices (``reward_field_indices``, e.g. (3, 4)) are routed to a reward-cleanup fn built from
    ``items_dict``. Passing the returned router as the quest hook's per-hook fn makes
    ``BlockingHook.serve_once`` apply the right fn per field. The OTHER hooks keep passing a plain
    callable and are unaffected.
    """
    default_fn, _ = build_translate_fn(
        cfg, translator, wrap_width=wrap_width, lines_per_page=lines_per_page,
        sync=sync, suppression=suppression,
    )
    reward_fn = build_rewards_translate_fn(items_dict)
    overrides = {i: reward_fn for i in reward_field_indices}
    return FieldRouter(default_fn, overrides)


def build_name_translate_fn(cfg, translator, *, prefix=""):
    """Return a ``translate_fn(ja) -> str | None`` for NAME surfaces (e.g. overhead nameplates).

    A name is a proper noun — machine-translating it mangles it — so this path never calls MT. It:
      1. returns None when ``ja`` isn't Japanese (leave it as-is);
      2. tries a whole-string community/cache hit (the same lookup the text path uses) — a curated
         NPC/monster name renders perfectly;
      3. else, if offline romanization is available, transliterates the name to romaji (player names
         can't live in any curated DB);
      4. else returns None (no romanizer, no hit — leave it Japanese).
    Names are single tokens, so there's no wrapping, pagination, or tag handling here.

    ``prefix`` is prepended to the WRITTEN value (never the lookup key) ONLY when a real replacement
    is produced — a pass-through ``None`` (non-Japanese, or no hit + no romanizer) is returned
    unchanged, never ``prefix`` alone. The NAMEPLATES surface passes ``prefix="\\x04"`` (ported from
    upstream app/hooking/hooks/nameplates.py:54, which returns ``"\\x04" + result``; per its comment
    on lines 50-53, without the \\x04 a replaced overhead name renders RED with a GM-avatar chat
    picture). The network_text name routing leaves ``prefix=""`` — upstream does NOT prefix the
    network_text name categories, only the nameplates hook.
    """
    community_lookup = _make_community_lookup(cfg, translator)

    def translate_fn(ja: str) -> str | None:
        if not is_japanese(ja):
            return None
        hit = community_lookup(ja)
        if hit:
            return prefix + hit
        if romanize.is_available():
            return prefix + romanize.romanize(ja)
        return None

    return translate_fn


# Category sets for the network_text template-string surface, copied VERBATIM from upstream
# app/hooking/hooks/network_text.py so our routing matches the game's known category taxonomy.
#
# * NET_TRANSLATE_CATEGORIES (upstream `_translate_categories`, lines 13-42): the whitelist. ONLY
#   these categories are ever touched; an unknown/non-whitelisted category passes through unchanged.
#   This is what stops battle text (player/monster names, action lines) from being machine-
#   translated and mangled every combat hit.
# * NET_IGNORE_CATEGORIES (upstream `_to_ignore`, lines 45-105): known-but-not-translated
#   categories (battle/UI noise, numbers, version strings) — always passed through.
# * NET_NAME_CATEGORIES (upstream NAME subset, lines 158-176): proper-noun categories that MUST use
#   the name path (community/cache hit, else offline romanization) — never MT, which mangles a name.
# * NET_GENERIC_CATEGORIES (upstream generic-string subset, lines 183-192): generic quest/item
#   strings; routed to the text path here.
NET_TRANSLATE_CATEGORIES = frozenset({
    "<%sM_pc>",
    "<%sM_npc>",
    "<%sL_SENDER_NAME>",
    "<%sB_TARGET_RPL>",
    "<%sM_00>",
    "<%sM_kaisetubun>",
    "<%sC_QUEST>",
    "<%sC_PC>",
    "<%sM_OWNER>",
    "<%sM_hiryu>",
    "<%sL_HIRYU>",
    "<%sL_HIRYU_NAME>",
    "<%sM_name>",
    "<%sM_02>",
    "<%sM_header>",
    "<%sM_item>",
    "<%sL_OWNER>",
    "<%sL_URINUSI>",
    "<%sM_NAME>",
    "<%sL_PLAYER_NAME>",
    "<%sL_QUEST>",
    "<%sC_ITMR_STITLE>",
    "<%sCAS_gambler>",
    "<%sCAS_target>",
    "<%sC_MERCENARY>",
    "<%sC_STR2>",
    "<%sL_MONSTERNAME>",
    "<%sEV_QUEST_NAME>",
})

NET_IGNORE_CATEGORIES = frozenset({
    "<%sM_Hankaku>",
    "<%sM_katagaki2>",
    "<%sW_MAP_NAME>",
    "<%sM_timei>",
    "<%sW_REP_MAX_2ND_R>",
    "<%sW_REP_MAX_2ND_F>",
    "<%sB_TARGET_ID>",
    "<%sM_mp_hp>",
    "<%sB_ITEM>",
    "<%sB_ACTOR_ID>",
    "<%sB_TARGET2_ID>",
    "<%sB_ACTION>",
    "<%sB_TARGET2>",
    "<%sB_renkin1>",
    "<%sB_kakko>",
    "<%sB_renkindiff>",
    "<%sB_plusminus>",
    "<%sM_plusnum>",
    "<%sB_VALUE>",
    "<%sB_VALUE2>",
    "<%sB_VALUE3>",
    "<%sB_VALUE4>",
    "<%sB_VALUE5>",
    "<%sB_VALUE6>",
    "<%sM_caption>",
    "<%sM_tuyosa>",
    "<%sParam1>",
    "<%sParam2>",
    "<%sParam3>",
    "<%sB_RANK>",
    "<%sM_rurastone>",
    "<%sM_sub>",
    "<%sM_dot>",
    "<%sM_TXT_00>",
    "<%sM_skill1>",
    "<%sM_01>",
    "<%sM_rare>",
    "<%sM_fugou>",
    "<%sM_num1>",
    "<%sM_emote>",
    "<%sM_3PLeader1>",
    "<%sM_3PLeader2>",
    "<%sM_3PLeader3>",
    "<%sC_STR1>",
    "<%s_MVER1>",
    "<%s_MVER2>",
    "<%s_MVER3>",
    "<%sW_DELIMITER>",
    "<%sM_slogan>",
    "<%sM_team>",
    "<%sM_monster>",
    "<%sM_speaker>",
    "<%sM_chat>",
    "<%sM_CW_stamp>",
    "<%sCAS_monster>",
    "<%sCAS_action>",
    "<%sB_ACTOR>",
    "<%sB_TARGET>",
    "<%sL_GOODS>",
})

NET_NAME_CATEGORIES = frozenset({
    "<%sM_pc>",
    "<%sM_npc>",
    "<%sC_PC>",
    "<%sL_SENDER_NAME>",
    "<%sM_OWNER>",
    "<%sM_hiryu>",
    "<%sL_HIRYU>",
    "<%sL_HIRYU_NAME>",
    "<%sM_name>",
    "<%sL_OWNER>",
    "<%sL_URINUSI>",
    "<%sM_NAME>",
    "<%sL_PLAYER_NAME>",
    "<%sCAS_gambler>",
    "<%sCAS_target>",
    "<%sC_MERCENARY>",
    "<%sL_MONSTERNAME>",
})

NET_GENERIC_CATEGORIES = frozenset({
    "<%sM_00>",
    "<%sC_QUEST>",
    "<%sM_02>",
    "<%sM_header>",
    "<%sM_item>",
    "<%sL_QUEST>",
    "<%sC_ITMR_STITLE>",
    "<%sC_STR2>",
    "<%sEV_QUEST_NAME>",
})

# Back-compat alias for the prior name (some callers/tests may import it). Aligned with upstream's 17.
NETWORK_NAME_CATEGORIES = NET_NAME_CATEGORIES

# NOVEL (no upstream equivalent): battle name-tags. A category CONTAINING any of these is a battle
# message whose captured Japanese argument is a monster/actor NAME (the standalone tags are in
# NET_IGNORE today and full templates aren't whitelisted, so both are dropped). When cfg.translate.
# battle_names is on, build_network_translate_fn routes such a category to _translate_name_runs (the
# name-ify pass) instead. Number-only battle templates (<%dB_VALUE> etc.) contain no name tag, so
# they are untouched.
BATTLE_NAME_TAGS = frozenset({"<%sB_ACTOR>", "<%sB_TARGET>", "<%sB_TARGET2>"})

# NOVEL (no upstream equivalent): NAME-bearing category substrings for the "translate the rest" model
# (cfg.translate.network_translate_all). A category CONTAINING any of these carries a captured
# Japanese argument that is a proper noun (player/NPC/monster/map name), which MUST route to the
# instant name-ify pass (_translate_name_runs: player/sibling substitution -> cache/community/offline
# romaji, NEVER MT — MT mangles names and would lag the combat hot path). Substrings (not exact
# categories) so battle templates like "\sしびれくらげ\mしびれくらげ\e ... <%sB_TARGET> ..." are caught
# by their embedded tag, and so the simple/map/casino name variants are all covered with one set.
# Derived from BATTLE_NAME_TAGS + NET_NAME_CATEGORIES + the 15-min capture's name-bearing categories.
# Each substring is verified against the real category lists in tests to NOT catch numeric/date/tag
# noise (e.g. "_NAME>" matches <%sM_NAME>/<%sL_HIRYU_NAME> but not <%sB_VALUE>; "M_name>" is the
# lowercase <%sM_name> not <%sW_MAP_NAME> which is caught by "W_MAP_NAME").
NAME_TAGS = frozenset({
    "B_ACTOR", "B_TARGET", "B_TARGET2",   # battle actor/target name slots
    "<%sM_pc>", "<%sM_npc>", "<%sC_PC>",  # player/NPC character names
    "M_name>", "_NAME>",                  # <%sM_name>, <%sM_NAME>/<%sL_SENDER_NAME>/<%sL_HIRYU_NAME>/...
    "SENDER_NAME", "M_OWNER", "L_OWNER",  # mail sender, item/bazaar owner
    "URINUSI", "HIRYU", "hiryu",          # bazaar seller, dragon mount name (<%sL_HIRYU>/<%sM_hiryu>)
    "MONSTERNAME", "MERCENARY",           # monster + mercenary names
    "CAS_gambler", "CAS_target",          # casino gambler/target names
    "W_MAP_NAME",                         # map/zone name
    "client_pc", "<%sM_monster>",         # client player + monster name surfaces
})


def _is_name_category(category: str) -> bool:
    """True when ``category`` carries a proper-noun (name) argument -> the instant name-ify pass.

    Substring match (see ``NAME_TAGS``) so battle templates that EMBED a name tag are caught, while
    numeric/date/<@M_..>/version noise categories are not. Used only by the network_translate_all
    ("translate the rest") routing; the legacy whitelist path is unaffected.
    """
    return any(t in category for t in NAME_TAGS)


# The Story So Far panel is narrower than the dialogue box; its Japanese is pre-wrapped to ~16-20
# full-width chars (≈40 half-width EN cols). 38 keeps EN safely inside the panel so no line clips.
# NOTE: the panel's ◄ N/N ► navigation is per story CHAPTER, not per <br> sub-page — inserting <br>
# does NOT add a page (verified live: the counter stayed 9/9). The panel is only ~9 lines tall, and
# our (wordier) Google MT often wraps to more lines than that, so the bottom of a long recap is cut
# off. There's no layout fix for that — it needs a more CONCISE translation (the human story_so_far
# static data, which is pre-condensed to fit, or Claude). So we wrap (no clipping) but do NOT
# paginate here.
KAISETUBUN_WRAP = 38
KAISETUBUN_BOX_LINES = 9  # the panel shows ~9 lines; a longer recap is cut off (it can't scroll)


def _mark_recap_cutoff(text: str) -> str:
    """Make a too-tall Story So Far recap's cut-off OBVIOUS instead of silent.

    The panel shows ~KAISETUBUN_BOX_LINES lines and can't scroll or sub-paginate, so a wordier MT
    recap loses its bottom lines with no indication. We can't fit it (that needs a concise static/
    Claude translation), so trim to the visible height and end the last visible line with ``...`` so
    the reader knows there's more. A recap that already fits is returned unchanged.
    """
    lines = text.split("\n")
    if len(lines) <= KAISETUBUN_BOX_LINES:
        return text
    kept = lines[:KAISETUBUN_BOX_LINES]
    last = kept[-1].rstrip()
    while last and len(last) + 3 > KAISETUBUN_WRAP:  # make room for "..." within the panel width
        last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
    kept[-1] = (last + "...") if last else "..."
    return "\n".join(kept)


def build_network_translate_fn(cfg, translator, *, wrap_width=None, lines_per_page=None, sync=None):
    """Return ``fn(ja, category) -> str | None`` for the network_text return-hook surface.

    Two routings, selected by ``cfg.translate.network_translate_all``:

    * TRUE (default) — the "translate the rest" model. The whitelist (NET_TRANSLATE_CATEGORIES) is
      DROPPED as redundant: ``is_japanese(ja)`` already filters the ~93 noise categories
      (numbers/dates/<@M_..> tags/English), name-bearing categories (``_is_name_category``) take the
      instant name-ify pass, NET_IGNORE stays dropped, and EVERY other Japanese category flows to the
      ASYNC text path. This is what stops the startup "Important Notice" body, community-board post
      titles, items, and unknown prose from being silently left Japanese. The prose/recap text fns are
      forced ``sync=False`` so a cache-miss enqueues + returns None WITHOUT lagging the game thread.
    * FALSE — the EXACT legacy whitelist routing below (kept verbatim for opt-out / upstream parity).

    Both share the name-ify pass (``_translate_name_runs``: player/sibling substitution first, then
    cache/community/offline romaji — never MT, so names are instant on the combat hot path).

    Legacy (FALSE) routing replicates upstream hooks/network_text.py's decision order, but with our
    translate paths. ``None`` means "pass through — leave the game's text untouched" (the
    ReturnHook.serve_once treats None / unchanged as no-write):

      1. non-Japanese ``ja`` -> None (leave as-is);
      2. login-screen version noise (category starts with ``Version <%s_MVER``) -> None;
      2b. NOVEL (no upstream equivalent), gated on ``cfg.translate.battle_names``: a category
         CONTAINING a battle name-tag (``BATTLE_NAME_TAGS``) -> the name-ify pass
         (``_translate_name_runs``: player/sibling substitution first, then cache/community/offline
         romaji — never MT). Runs before the NET_IGNORE/whitelist checks because the standalone battle
         name tags are in NET_IGNORE and full battle templates aren't whitelisted (both dropped today);
      3. a NET_IGNORE category (battle/UI noise) -> None;
      4. ``ja`` ending in ``自分`` -> "<...>self" (the "<name> uses X on 自分/self" nicety);
      5. a category NOT in the whitelist (NET_TRANSLATE_CATEGORIES) -> None. This is what stops
         battle text: unknown/non-whitelisted categories are never machine-translated;
      6. a NAME category -> the name path (community/cache hit, else offline romanization, never MT);
      7. otherwise (whitelisted generic-string + ``<%sM_kaisetubun>`` story-so-far) -> the text path
         (community/cache whole-string hit, else MT via translate_conversation).

    NOTE: step 7's MT fallback deviates from upstream (which is static-data-only). This is an
    INTENTIONAL interim fallback so the Story So Far + quest/item strings keep translating until
    their static data is imported. Both paths reuse the existing build_*_translate_fn internals
    (no duplicated placeholder/community logic).
    """
    translate_all = getattr(cfg.translate, "network_translate_all", True)
    # CRITICAL — in the "translate the rest" model the DEFAULT path now sends arbitrary prose (the
    # startup notice, board post titles, unknown categories) to MT. That MUST be ASYNC so a cache-miss
    # returns None + enqueues WITHOUT blocking the game thread (build_translate_fn -> translate_
    # conversation with sync=False: request() then None on miss). The name path needs no MT (instant),
    # so only the text/recap fns are forced async here. The legacy whitelist path keeps the caller's
    # ``sync`` (network_text's HookSpec sync=True) unchanged.
    text_sync = False if translate_all else sync

    name_fn = build_name_translate_fn(cfg, translator)
    text_fn, _ = build_translate_fn(
        cfg, translator, wrap_width=wrap_width, lines_per_page=lines_per_page, sync=text_sync
    )
    # The "Story So Far" recap (<%sM_kaisetubun>) renders in a NARROWER, ~9-line, non-scrolling panel.
    # Wrap at KAISETUBUN_WRAP so long lines don't clip off the right edge (no <br>: the panel doesn't
    # paginate on it). A recap taller than the panel is then marked with a trailing "..." cutoff
    # indicator (see _mark_recap_cutoff) since we can't fit a wordy MT into the box.
    kaisetubun_fn, _ = build_translate_fn(
        cfg, translator, wrap_width=KAISETUBUN_WRAP, lines_per_page=lines_per_page, sync=text_sync
    )

    def translate_all_fn(ja: str, category: str) -> str | None:
        # "Translate the rest" routing: drop the redundant whitelist. is_japanese already filters the
        # ~93 noise categories; name-bearing categories take the instant name-ify pass; NET_IGNORE
        # stays dropped (high-volume JP chat/UI noise); every OTHER Japanese category flows to the
        # ASYNC text path (so the startup notice, board titles, items, unknown prose translate instead
        # of staying Japanese) rather than being silently dropped.
        if not is_japanese(ja):
            return None
        if category.startswith("Version <%s_MVER"):
            return None
        if ja.endswith("自分"):
            return ja[:-2] + "self"
        if _is_name_category(category):
            return _translate_name_runs(ja, translator)  # player-sub aware; instant (no MT)
        if category in NET_IGNORE_CATEGORIES:
            return None  # explicit high-volume JP noise (chat etc.)
        if category == "<%sM_kaisetubun>":
            recap = kaisetubun_fn(ja)  # narrower wrap so it doesn't clip the panel's right edge
            return _mark_recap_cutoff(recap) if recap else recap
        return text_fn(ja)  # DEFAULT: community -> cold-async MT (notice, board posts, items, prose)

    if translate_all:
        return translate_all_fn

    def translate_fn(ja: str, category: str) -> str | None:
        if not is_japanese(ja):
            return None
        if category.startswith("Version <%s_MVER"):
            return None
        # NOVEL (no upstream equivalent): name-ify battle monster/actor names. A category CONTAINING a
        # battle name-tag routes to the name-ify pass (player/sibling substitution first, then cache/
        # community/offline-romaji — NO MT, so it's instant on the combat hot path; the \m…\e id is
        # name-ified too, pending live verification). This runs BEFORE the NET_IGNORE/whitelist checks
        # because the standalone <%sB_TARGET>/<%sB_ACTOR> tags are in NET_IGNORE and full battle
        # templates aren't whitelisted, so both are dropped today. Number-only battle templates
        # (<%dB_VALUE> etc.) carry no name tag and are untouched. Gated on the toggle: when
        # battle_names is False this branch is skipped entirely (exact current behaviour).
        if cfg.translate.battle_names and any(
            t in category for t in BATTLE_NAME_TAGS
        ):
            return _translate_name_runs(ja, translator)
        if category in NET_IGNORE_CATEGORIES:
            return None
        if ja.endswith("自分"):
            # "self" text when player/monster uses a spell on themselves.
            return ja[:-2] + "self"
        if category not in NET_TRANSLATE_CATEGORIES:
            # Unknown/non-whitelisted -> pass through. This stops battle text being MT'd/mangled.
            return None
        if category in NET_NAME_CATEGORIES:
            return name_fn(ja)
        if category == "<%sM_kaisetubun>":
            recap = kaisetubun_fn(ja)  # narrower wrap so it doesn't clip the panel's right edge
            return _mark_recap_cutoff(recap) if recap else recap  # mark the bottom cut-off if any
        return text_fn(ja)

    return translate_fn


def serve(
    mem, hooks, *, stop: threading.Event, on_line=None,
    game_gone: threading.Event | None = None,
) -> int:
    """Poll all ``(name, hook, fn)`` triples until ``stop`` is set.

    ``hook`` is any of ``BlockingHook | ReturnHook | PlayerHook`` (anything exposing
    ``serve_once``/``restore``), and ``fn`` is that hook's per-surface callback: a ``translate_fn``
    for the text/name surfaces, or ``apply_names`` for the read-only PLAYER hook. ``serve_once``
    returns non-None only when there was something to report (a translated field, or — for the
    PLAYER hook — a real name change), at which point ``on_line(name, value)`` fires.

    Each text hook carries its OWN ``translate_fn`` so different surfaces can use different format
    profiles (e.g. dialogue paginates with <br> and translates synchronously; the quest menu uses
    no <br> and translates asynchronously). Returns the number of fields served.

    GAME-LIFECYCLE SAFETY: every ``serve_once`` reads the game's memory, which can fail two ways:

      * the GAME IS GONE (closed/crashed) — ``mem.read`` returns ``b""``, so ``read_u32`` raises
        ``struct.error`` (or a ``/proc/<pid>/mem`` fallback raises ``OSError``). We confirm with the
        cheap ``mem.is_alive()`` probe; if the pid is gone we set ``game_gone`` (if provided), set
        ``stop``, and return cleanly so the supervisor can RE-ATTACH when the game returns — no more
        crashing the service with a traceback.
      * a TRANSIENT read blip on a still-running game — same exceptions, but ``mem.is_alive()`` is
        True. We skip just that hook for this tick and keep serving (one bad read must not crash us).
    """
    served = 0
    while not stop.is_set():
        idle = True
        for name, hook, translate_fn in hooks:
            try:
                ja = hook.serve_once(mem, translate_fn)
            except (struct.error, OSError):
                # A read failed. Distinguish "game is gone" from a one-off blip with a cheap probe.
                if not mem.is_alive():
                    if game_gone is not None:
                        game_gone.set()
                    stop.set()
                    return served  # CLEAN exit — the game is gone; the supervisor re-attaches.
                continue  # still alive -> transient blip: skip this hook this tick and carry on.
            if ja is not None:
                served += 1
                idle = False
                if on_line:
                    on_line(name, ja)
        if idle:
            stop.wait(0.001)  # only sleep when nothing is pending (keeps block latency low)
    return served
