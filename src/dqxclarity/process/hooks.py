"""Declarative hook registry — one entry per translatable text surface.

Each surface (dialogue, quests, …) is the same shape: scan for a function signature, detour its
prologue with a blocking hook, and translate the text field(s) reachable from the captured arg0.
Adding a surface = adding a ``HookSpec`` (signature + field offsets), not new bespoke code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import detour, signatures as sig
from .memory_linux import LinuxProcessMemory


@dataclass(frozen=True)
class HookSpec:
    name: str
    signature: bytes            # literal AOB to scan for (re.escaped before matching)
    stolen_len: int             # whole-instruction bytes to steal for the detour (>= 5)
    # (struct offset, max writable bytes) per text field reachable from the captured arg0 pointer.
    fields: tuple[tuple[int, int], ...] = ((0, 1 << 20),)  # default: a single unbounded buffer
    prologue_back: int = 0      # bytes to subtract from the match to reach the prologue
    prologue_verify: bytes = b""  # expected bytes at the prologue (b"" = skip the check)
    # Per-surface format profile (defaults are the dialogue box's: wrap 46, <br> every 3 lines,
    # synchronous first-view MT). Other surfaces override these — e.g. the quest menu renders
    # <br> literally and must not block the menu on a slow translation.
    wrap_width: int = 46        # characters per line before wrapping
    lines_per_page: int = 3     # insert <br> every N lines (<1 = no <br> pagination)
    sync: bool = True           # translate on the blocking hot path (False = async/background)
    # A wildcard byte-regex (matched raw, NOT re.escaped) used instead of ``signature`` when the
    # function's bytes vary across builds (e.g. a call rel32). When None, ``signature`` is escaped
    # and matched literally as before.
    pattern: bytes | None = None
    # Shellcode that loads the text-buffer address into eax for the blocking handshake. Defaults to
    # reading arg0 at a prologue; a call-site surface (walkthrough) reads the returned object ptr.
    capture: bytes = detour.CAPTURE_ARG0
    # True for surfaces whose captured text is a NAME (e.g. overhead nameplates). Names must use the
    # name translate path (community/cache hit, else offline romanization) — never machine
    # translation, which mangles a proper noun. False = the regular text translate path.
    is_name: bool = False
    # True for surfaces that must read the result AFTER the function returns (network_text "Story So
    # Far"/NPC names/quest template strings). These install a return hook (entry+exit shellcode with a
    # shadow stack) instead of a prologue blocking hook, and their translate fn takes (ja, category).
    return_hook: bool = False
    # True for the player-login hook: a READ-ONLY blocking hook that reads the player + sibling names
    # out of the captured login struct and applies them to our config/translator. It installs the SAME
    # blocking detour, but its serve fn is apply_names(player_ja, sibling_ja, relationship) and it NEVER
    # writes into the game (only the STATE-release dword) — so it can't corrupt/crash the game.
    player: bool = False
    # Indices into ``fields`` whose text is a STRUCTURED quest-reward list (questRewards /
    # questRepeatRewards), NOT prose. These must be cleaned per-line by the reward-cleanup fn
    # (translate.rewards.clean_quest_rewards) instead of run through the whole-string translate fn,
    # which mangles the list. The dispatch layer builds a per-field router (FieldRouter) that applies
    # the reward fn to exactly these field indices and the normal translate fn to the rest. Empty
    # (the default) = every field uses the same fn (backward compatible: the other 6 hooks unaffected).
    reward_field_indices: tuple[int, ...] = ()


# The registry. Signatures/offsets ported from upstream hooking/scripts/*.ts + hook.py.
HOOKS: dict[str, HookSpec] = {
    "dialogue": HookSpec(
        name="dialogue",
        signature=sig.DIALOGUE_FUNC,
        stolen_len=sig.DIALOGUE_STOLEN_LEN,
        fields=((0, 1 << 20),),  # arg0 is the text pointer directly (standalone buffer)
    ),
    "quest": HookSpec(
        name="quest",
        signature=sig.QUEST_SIG,
        stolen_len=sig.QUEST_STOLEN_LEN,
        fields=sig.QUEST_FIELDS,  # arg0 is a struct; text at these offsets
        prologue_back=sig.QUEST_PROLOGUE_BACK,
        prologue_verify=b"\x55\x8b\xec",
        # The quest menu renders <br> literally (it doesn't paginate on it like the dialogue box),
        # so lines_per_page=0 disables page-break insertion. It also reads several fields per open;
        # translating them synchronously would freeze the menu for ~1-2s, so sync=False keeps the
        # menu responsive (a slow line stays JA this pass and shows EN once cached).
        wrap_width=46,
        lines_per_page=0,
        sync=False,
        # Offsets 640 (index 3, questRewards) and 744 (index 4, questRepeatRewards) are STRUCTURED
        # reward lists — route them through the per-line reward-cleanup fn, not the prose translate
        # fn. The other quest fields (name/description) keep the normal path. See sig.QUEST_FIELDS.
        reward_field_indices=(3, 4),
    ),
    "walkthrough": HookSpec(
        name="walkthrough",
        signature=b"",  # unused: this surface matches via the wildcard `pattern` below
        pattern=sig.WALKTHROUGH_PATTERN,
        stolen_len=sig.WALKTHROUGH_STOLEN_LEN,
        fields=((0, 1 << 20),),  # captured addr is the text buffer itself (eax+0xEC); single buffer
        prologue_back=sig.WALKTHROUGH_PROLOGUE_BACK,  # -5: hook the lea just past the call
        prologue_verify=b"\x8d\xb8",                  # lea edi,[eax+disp32]
        capture=sig.WALKTHROUGH_CAPTURE,              # read returned object ptr + 0xEC
        # The "Story So Far" panel: upstream wraps at 31 chars, no <br>. It reads one field on open,
        # so a synchronous first-view translation (community hit, else MT) is fine — no menu freeze.
        wrap_width=31,
        lines_per_page=0,
        sync=True,
    ),
    "corner_text": HookSpec(
        name="corner_text",
        signature=b"",  # unused: matched via the wildcard `pattern` below
        pattern=sig.CORNER_TEXT_PATTERN,
        stolen_len=sig.CORNER_TEXT_STOLEN_LEN,
        fields=((0, 1 << 20),),  # captured arg (a4) is the text buffer itself; single buffer
        prologue_back=0,            # the match IS the function prologue (55 8B EC)
        prologue_verify=b"\x55\x8b\xec",
        capture=detour.CAPTURE_ARG2,  # text ptr is the 3rd stack arg (a4) = [esp+0x30]
        # Top-right NPC text: short, renders <br> literally (no pagination -> lines_per_page=0). It's
        # ordinary NPC text, so it uses the regular text translate path (community hit, else MT).
        wrap_width=46,
        lines_per_page=0,
        sync=True,
    ),
    "nameplates": HookSpec(
        name="nameplates",
        signature=b"",  # unused: matched via the wildcard `pattern` below
        pattern=sig.NAMEPLATES_PATTERN,
        stolen_len=sig.NAMEPLATES_STOLEN_LEN,
        fields=((0, 1 << 20),),  # captured arg (a2) is the name buffer itself; single buffer
        prologue_back=0,            # the match IS the function prologue (55 8B EC)
        prologue_verify=b"\x55\x8b\xec",
        capture=detour.CAPTURE_ARG0,  # name ptr is the 1st stack arg (a2) = [esp+0x28]
        # Overhead entity/NPC names: short, no pagination. is_name=True routes this through the NAME
        # translate path (community/cache hit, else offline romanization) — never MT, which would
        # mangle a proper noun.
        wrap_width=46,
        lines_per_page=0,
        sync=True,
        is_name=True,
    ),
    "network_text": HookSpec(
        name="network_text",
        signature=b"",  # unused: matched via the wildcard `pattern` below
        pattern=sig.NETWORK_TEXT_PATTERN,
        stolen_len=sig.NETWORK_TEXT_STOLEN_LEN,
        fields=((0, 1 << 20),),  # the result string is read from the context fields, not a struct offset
        prologue_back=0,            # the match IS the function prologue (55 8B EC ...)
        prologue_verify=b"\x55\x8b\xec",
        return_hook=True,           # read the result AFTER the function returns (eax==1)
        # Story-so-far recap + NPC names + quest template strings. Mixed content routed per category by
        # build_network_translate_fn; the text path wraps at 46, renders <br> literally (no pagination),
        # and translates synchronously on the (return) hot path.
        wrap_width=46,
        lines_per_page=0,
        sync=True,
    ),
    "player": HookSpec(
        name="player",
        signature=sig.PLAYER_SIG,        # literal AOB (no wildcards)
        stolen_len=sig.PLAYER_STOLEN_LEN,
        # The match IS the function prologue (55 8B EC ...); verify it and capture arg0 (the struct
        # address = the 1st stack arg a2). The blocking detour must hold the login thread so the
        # struct stays valid while we read three strings out of it; an async/ring read could see a
        # freed struct. fields/wrap/lpp/sync/is_name are irrelevant — the serve action is apply_names,
        # not translate+writeback (this is a READ-ONLY hook; it never writes into the game).
        prologue_back=0,
        prologue_verify=b"\x55\x8b\xec",
        capture=detour.CAPTURE_ARG0,
        player=True,
    ),
}


@dataclass
class FoundHook:
    spec: HookSpec
    func_addr: int


def find_function(mem: LinuxProcessMemory, spec: HookSpec) -> int | None:
    """Resolve the function prologue address for ``spec``, or None if not found/ambiguous.

    Scans for the (literal) signature; requires exactly one match; applies ``prologue_back`` and
    verifies the prologue bytes. re.escape keeps signature bytes that are regex metacharacters
    (e.g. 0x5B '[', 0x5E '^') from being interpreted by the pattern scanner.
    """
    pat = spec.pattern if spec.pattern is not None else re.escape(spec.signature)
    matches = mem.pattern_scan(pat, data_only=False, limit=4) or []
    if len(matches) != 1:
        return None
    func = matches[0] - spec.prologue_back
    if spec.prologue_verify and mem.read(func, len(spec.prologue_verify)) != spec.prologue_verify:
        return None
    return func


def locate(mem: LinuxProcessMemory, names: list[str] | None = None) -> list[FoundHook]:
    """Find the functions for the named hooks (all if None). Skips ones that don't resolve."""
    specs = [HOOKS[n] for n in (names or HOOKS)]
    out: list[FoundHook] = []
    for spec in specs:
        addr = find_function(mem, spec)
        if addr is not None:
            out.append(FoundHook(spec, addr))
    return out


def install(mem: LinuxProcessMemory, found: FoundHook):
    """Install the detour for ``found``. Returns a hook with ``.serve_once(mem, fn)`` + ``.restore``.

    Return-hook surfaces (network_text) install a return hook (entry+exit shellcode + shadow stack)
    whose ``serve_once`` calls the translate fn with (ja, category); the player-login surface installs
    a READ-ONLY blocking hook whose ``serve_once`` calls apply_names(player_ja, sibling_ja,
    relationship); all other surfaces install the prologue blocking hook whose ``serve_once`` calls it
    with (ja). All shapes drive ``serve()`` uniformly.
    """
    if found.spec.return_hook:
        return detour.install_return_hook(
            mem, found.func_addr, stolen_len=found.spec.stolen_len, fields=found.spec.fields,
        )
    if found.spec.player:
        # Same blocking detour + cave as a text hook (it must BLOCK so the struct stays valid while
        # we read it), but a READ-ONLY serve action — it never writes back into the game.
        return detour.install_player_hook(
            mem, found.func_addr, stolen_len=found.spec.stolen_len, capture=found.spec.capture,
        )
    return detour.install_blocking_hook(
        mem, found.func_addr, stolen_len=found.spec.stolen_len, fields=found.spec.fields,
        capture=found.spec.capture,
    )
