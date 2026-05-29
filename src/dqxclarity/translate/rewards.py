"""Quest-reward field cleanup (pure) — mirror of upstream clean_up_and_return_items.

The quest window's reward fields (questRewards / questRepeatRewards) are a STRUCTURED list, not a
whole prose string: one reward per line, each a bullet + item name + quantity suffix, sometimes
prefixed with a male/female marker. Running the whole field through the dialogue translate path
mangles it. Instead we process EACH line: strip the gender prefix and bullet, parse the quantity
suffix, look the cleaned item name up in a JA-item-name -> EN-item-name dict, and re-emit it
right-aligned as ``・{en}{padding}{qty}``.

Ported faithfully from dqxclarity/app/common/translate.py:502-552 (clean_up_and_return_items). The
item dictionary is supplied by the caller (built from items.json + key_items.json +
custom_quest_rewards.json via ``community.build_reward_items_dict``), so this module stays pure and
testable — no DB or network.
"""

from __future__ import annotations

import re
import unicodedata

# Lines ending in 他 ("etc.") normally mean "(1) item shown, plus others"; but a skill-learn reward
# ("...必殺技を覚える" / "...入れられるよう...") ends in 他 too and must NOT get a "(1)" count — its
# quantity is blank. Mirrors upstream's `bad_strings` list (translate.py:524).
_SKILL_LEARN_KEYWORDS = ("必殺技を覚える", "入れられるよう")

# Right-alignment target column for the quantity, ported from upstream's `31 - ...` (translate.py:533).
_ALIGN_WIDTH = 31


def _quantity_for(no_bullet: str) -> str:
    """Parse the quantity suffix of a (bullet-stripped) reward line, mirroring upstream.

    * ends with こ -> the count is the two chars before こ, NFKC-normalized (fullwidth digits ->
      ascii) with spaces removed, wrapped as ``(N)`` (translate.py:520-522);
    * ends with 他 -> ``(1)`` UNLESS the line is a skill-learn reward, in which case ``""``
      (translate.py:523-525);
    * otherwise -> ``""``.
    """
    if no_bullet.endswith("こ"):
        qty = "(" + unicodedata.normalize("NFKC", no_bullet[-3:-1]) + ")"
        return re.sub(" ", "", qty)
    if no_bullet.endswith("他"):
        return "" if any(k in no_bullet for k in _SKILL_LEARN_KEYWORDS) else "(1)"
    return ""


def _format_line(value: str, quantity: str, had_bullet: bool) -> str:
    """Right-align one resolved reward line: ``[・]{value}{padding}{quantity}`` (upstream :530-543).

    Padding = 31 - len(en) - len(qty) - (utf8_overhead / 2), where utf8_overhead is the EN string's
    byte length minus its char length (so multibyte EN — rare — is accounted for, exactly as
    upstream does with ``(byte_count - value_length) // 2``). A bullet is re-emitted only when the
    source line had one.
    """
    value_length = len(value)
    quant_length = len(quantity)
    byte_count = len(value.encode("utf-8"))
    num_spaces = _ALIGN_WIDTH - value_length - quant_length - ((byte_count - value_length) // 2)
    if num_spaces < 0:
        num_spaces = 0  # never emit a negative-width pad (upstream relies on values fitting in 31)
    prefix = "・" if had_bullet else ""
    return prefix + value + (" " * num_spaces) + quantity


def clean_quest_rewards(text: str, items_dict: dict[str, str]) -> str:
    """Clean a quest reward field, one line per reward (mirror of clean_up_and_return_items).

    For each line: strip the male/female prefix (``男は ``/``女は `` and their fullwidth-space
    variants) and the leading bullet ``・``, parse the quantity suffix (こ -> ``(N)``; 他 -> ``(1)``
    or ``""`` for skill-learn rewards), drop a trailing ``　　…`` annotation, and look the cleaned
    item name up in ``items_dict``. On a hit, re-emit the line right-aligned as ``・{en}{pad}{qty}``.

    Fallbacks mirror upstream exactly:
      * a single-line field (no newline) whose item ISN'T found returns the WHOLE original ``text``
        UNLESS the line contains ``討伐ポイント``, in which case it returns ``・Experience Points`` +
        the points substring (chars 6:18 of the bulletless line);
      * a multi-line field keeps an unresolved line verbatim and joins with newlines (rstripped).

    Returns the cleaned multi-line string (or the single-line result / original text on a miss).
    """
    line_count = text.count("\n")
    # Strip male/female reference prefixes (regular + fullwidth space), upstream :511-514.
    sanitized = re.sub("男は ", "", text)
    sanitized = re.sub("女は ", "", sanitized)
    sanitized = re.sub("男は　", "", sanitized)
    sanitized = re.sub("女は　", "", sanitized)

    final_string = ""
    for item in sanitized.split("\n"):
        no_bullet = re.sub(r"(^\・)", "", item)
        points = no_bullet[6:18]
        quantity = _quantity_for(no_bullet)
        # Drop a trailing "　　<annotation>" (fullwidth-space-delimited note), upstream :526.
        no_bullet = re.sub("(　　.*)", "", no_bullet)

        if no_bullet in items_dict:
            value = items_dict.get(no_bullet)
            if value:
                had_bullet = "・" in item
                line = _format_line(value, quantity, had_bullet)
                if line_count == 0:
                    return line
                final_string += line + "\n"
        else:
            if line_count == 0:
                if "討伐ポイント" in item:
                    return "・" + "Experience Points" + points
                return text  # single-line miss -> leave the field untouched (upstream :549)
            final_string += item + "\n"  # multi-line miss -> keep the line verbatim (upstream :551)
    return final_string.rstrip()
