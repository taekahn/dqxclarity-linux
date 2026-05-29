"""AOB / byte-regex patterns used to locate text in the live game.

Ported verbatim from upstream dqxclarity (``app/common/signatures.py``). These are byte-regex
patterns matched with ``re.DOTALL`` (so ``.`` matches any byte). Data patterns (names, notices)
are not code signatures and shift across game patches; keep them in sync with upstream.
"""

from __future__ import annotations

# Custom memchr-like routine the game uses; some JA strings are only visible during network
# handling and only pass through here. Code signature (stable-ish across patches).
MEM_CHR_TRIGGER = rb"\x8B\x44\x24\x0C\x53\x85\xC0\x74\x52"

# Dialogue text function prologue (__thiscall; this=ecx, arg0=text ptr, arg1=npc name ptr).
# We steal the first 6 bytes (push ebp; mov ebp,esp; push esi; mov esi,ecx) for the detour.
#   55 8B EC 56 8B F1 80 BE EC000000 00 7407 C686 ED000000 01 FF7518 8B450C 51
DIALOGUE_FUNC = bytes.fromhex(
    "55" "8BEC" "56" "8BF1" "80BE" "EC000000" "00" "7407"
    "C686" "ED000000" "01" "FF7518" "8B450C" "51"
)
DIALOGUE_STOLEN_LEN = 6

# Quest function (__thiscall CopyQuestData(struct* arg0, int)). Found by a TAIL signature, then
# `match - 0x115` reaches the prologue (verified to be `55 8B EC`). arg0 is a struct pointer; the
# translatable text fields sit at fixed offsets within it (see QUEST_FIELDS). Ported from upstream
# scripts/quest.ts + hook.py.
QUEST_SIG = bytes.fromhex("888657030000" "5e5b5d" "c20400")
QUEST_PROLOGUE_BACK = 0x115
QUEST_STOLEN_LEN = 7  # push ebp; mov ebp,esp; push ebx; mov ebx,[ebp+8]  (1+2+1+3)
# (struct offset, max writable bytes) per text field. The max = the gap to the next field, so a
# long translation of one field can never overflow into the next (invariant I1). Last field's size
# is unknown, so use a conservative bound.
QUEST_FIELDS = ((20, 56), (76, 56), (132, 508), (640, 104), (744, 256))

# Walkthrough / "The Story So Far" text. Unlike the prologue hooks, upstream detours the instruction
# right AFTER a call: at that site eax = the returned object pointer and the text buffer is at
# eax+0xEC. So we hook `match + 5` (just past the E8 call), i.e. WALKTHROUGH_PROLOGUE_BACK = -5, and
# verify the lea opcode (8D B8). The pattern wildcards the call's rel32 and the lea displacement
# (both build-specific) — `....` matches any 4 bytes under re.DOTALL. We steal the 6-byte lea
# `lea edi,[eax+0xEC]`. Ported from upstream scripts/walkthrough.ts (wrap 31, no <br>).
WALKTHROUGH_PATTERN = rb"\xE8....\x8D\xB8....\x8B\xCF\x8D\x51"
WALKTHROUGH_PROLOGUE_BACK = -5     # hook addr = match + 5 (the lea after the call)
WALKTHROUGH_STOLEN_LEN = 6         # lea edi,[eax+0xEC]  = 8D B8 EC 00 00 00
WALKTHROUGH_TEXT_OFFSET = 0xEC     # text buffer = returned object ptr (eax) + 0xEC
# Capture for the blocking handshake: after pushad+pushfd the original eax (object ptr) sits at
# [esp+0x20]; load it and add the text offset so eax holds the text-buffer address.
#   8B 44 24 20            mov eax,[esp+0x20]   ; saved eax = returned object ptr
#   05 EC 00 00 00         add eax, 0xEC        ; -> text buffer
WALKTHROUGH_CAPTURE = b"\x8b\x44\x24\x20" + b"\x05" + WALKTHROUGH_TEXT_OFFSET.to_bytes(4, "little")

# Nameplates: __thiscall UpdateEntityNameplate(this=ecx, a2=name_ptr, a3). Prologue hook; the name
# pointer is the 1st stack arg (a2) = [esp+0x28] (CAPTURE_ARG0). Stolen 10 bytes:
#   55 push ebp | 8B EC mov ebp,esp | 56 push esi | 8B B1 88010000 mov esi,[ecx+0x188]
NAMEPLATES_PATTERN = rb"\x55\x8b\xec\x56\x8b\xb1....\x85\xf6\x74.\x8b\x45"
NAMEPLATES_STOLEN_LEN = 10

# Corner text (top-right NPC text): __thiscall sub(this=ecx, a2, a3, a4=text_ptr, ...). Prologue hook;
# the text pointer is the 3rd stack arg (a4) = [esp+0x30] (CAPTURE_ARG2). Stolen 6 bytes:
#   55 push ebp | 8B EC mov ebp,esp | 8B 45 10 mov eax,[ebp+0x10]
CORNER_TEXT_PATTERN = rb"\x55\x8b\xec\x8b\x45.\x83\xec.\x53\x8b\x5d.\x56\x8b\xf1\x57\x85\xc0"
CORNER_TEXT_STOLEN_LEN = 6

# network_text: bool __cdecl ProcessTemplateString(a1=context, ...). RETURN hook (read result after
# the call). On return, if eax==1: len@[a1+0x10], end@[a1+0x18], category@[a1+0x1c]; string start =
# end - len (explicit length, NOT null-terminated). Ported from upstream scripts/network_text.ts.
NETWORK_TEXT_PATTERN = rb"\x55\x8b\xec\x81\xec....\xa1....\x33\xc5\x89\x45.\x8b\x45.\x8b\x0d....\x89\x45.\x64\xa1"
# The prologue is 55 (push ebp, 1) | 8B EC (mov ebp,esp, 2) | 81 EC ???????? (sub esp,imm32, 6) = 9
# whole bytes for three whole instructions. We need >=5 on whole-instruction boundaries, so steal 9.
# The 4-byte sub-immediate is wildcarded in the pattern but the instruction length is fixed at 6.
NETWORK_TEXT_STOLEN_LEN = 9
# Context field offsets (from a1): u32 length @ +0x10, u32 end-of-buffer addr @ +0x18, char* category
# @ +0x1c. String START = end - length, and it is NOT null-terminated (use the explicit length).
NETWORK_TEXT_LEN_OFFSET = 0x10
NETWORK_TEXT_END_OFFSET = 0x18
NETWORK_TEXT_CATEGORY_OFFSET = 0x1C

# Player login: __thiscall sub(this=ecx, a2=struct_addr). READ-ONLY hook — reads the player +
# sibling names from the login struct (names at +24/+100, relationship byte at +119). The struct
# address is the 1st stack arg (a2), captured as CAPTURE_ARG0. Stolen prologue = 6 bytes:
#   55 push ebp | 8B EC mov ebp,esp | 56 push esi | 8B F1 mov esi,ecx
# Signature is literal (no wildcards): 55 8B EC 56 8B F1 57 8B 46 58 85 C0. Ported from upstream
# scripts/player.ts + hooks/player.py.
PLAYER_SIG = bytes.fromhex("55 8B EC 56 8B F1 57 8B 46 58 85 C0".replace(" ", ""))
PLAYER_STOLEN_LEN = 6
PLAYER_NAME_OFFSET = 24
PLAYER_SIBLING_OFFSET = 100
PLAYER_RELATIONSHIP_OFFSET = 119

# ---- data patterns (volatile; re-verify after game patches) ---------------- #

# Concierge / NPC name. (D8 E5 ?? ?? ?? ?? ?? ?? 68 0C ?? ?? E?)
CONCIERGE_NAME = rb"\xD8\xE5......\x68\x0C..[\xE3\xE4\xE5\xE6\xE7\xE8\xE9\xEF]"

# Menu AI (party member) names. (58 bytes)
MENU_AI_NAME = (
    rb"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    rb"...........\x00..\x00\x00.....\x00\x00\x00.[\x1B\x1C].....\x00.....\x00..[\xE3\xEF]"
)

# Comm (chat/communication) names. (32 bytes)
COMM_NAME = rb"[\xE3\xEF].................\x00\x00\x0F\x00\x00\x00\x01\x02\x00\x00\x01\x00\x00"

# "動画配信の際はサーバー" — appears in the login notice box (UTF-8 bytes of the phrase).
NOTICE_STRING = (
    rb"\xE5\x8B\x95\xE7\x94\xBB\xE9\x85\x8D\xE4\xBF\xA1\xE3\x81\xAE"
    rb"\xE9\x9A\x9B\xE3\x81\xAF\xE3\x82\xB5\xE3\x83\xBC\xE3\x83\x90\xE3\x83\xBC"
)


from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class NamePattern:
    """A name pattern + how to reach and rewrite the name relative to the match.

    ``name_offset`` is bytes from the match start to the null-terminated JA name.
    ``write_prefix`` is control bytes the game expects prepended on write-back (upstream prefixes
    \\x04 for concierge/comm names).
    """

    name: str
    pattern: bytes
    name_offset: int
    write_prefix: str = ""


# Offsets/prefixes ported from upstream scans/{npc,player,comms}.py.
NAME_PATTERNS: list[NamePattern] = [
    NamePattern("concierge_name", CONCIERGE_NAME, 12, "\x04"),
    NamePattern("menu_ai_name", MENU_AI_NAME, 57, ""),
    NamePattern("comm_name", COMM_NAME, 0, "\x04"),
]
