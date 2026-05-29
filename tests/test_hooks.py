"""Tests for the HookSpec function-finder (signature scan + prologue-back + verify)."""

from __future__ import annotations

from dqxclarity.process.hooks import HOOKS, HookSpec, find_function

QUEST_BACK = 0x115


class FakeScanMem:
    """Stubs pattern_scan (returns preset matches) and read (returns preset prologue bytes)."""

    def __init__(self, matches: list[int], prologue: dict[int, bytes]) -> None:
        self._matches = matches
        self._prologue = prologue

    def pattern_scan(self, pattern, *, data_only=False, limit=4):  # noqa: ARG002
        return self._matches

    def read(self, addr: int, size: int) -> bytes:
        return self._prologue.get(addr, b"")[:size]


def _spec(**kw) -> HookSpec:
    base = dict(name="q", signature=b"\x88\x86\x57", stolen_len=7, fields=((0, 64),),
                prologue_back=QUEST_BACK, prologue_verify=b"\x55\x8b\xec")
    base.update(kw)
    return HookSpec(**base)


def test_find_function_applies_prologue_back_and_verifies():
    match = 0x00500000
    func = match - QUEST_BACK
    mem = FakeScanMem([match], {func: b"\x55\x8b\xec"})
    assert find_function(mem, _spec()) == func


def test_find_function_rejects_prologue_mismatch():
    match = 0x00500000
    mem = FakeScanMem([match], {match - QUEST_BACK: b"\x90\x90\x90"})  # not 55 8b ec
    assert find_function(mem, _spec()) is None


def test_find_function_rejects_ambiguous_match():
    mem = FakeScanMem([0x1000, 0x2000], {})  # two matches -> not unique
    assert find_function(mem, _spec()) is None


def test_find_function_rejects_no_match():
    assert find_function(FakeScanMem([], {}), _spec()) is None


def test_dialogue_spec_has_no_prologue_back():
    # dialogue is found by its own prologue, so the match IS the function (no back/verify)
    d = HOOKS["dialogue"]
    assert d.prologue_back == 0 and d.fields[0][0] == 0  # single field at offset 0
    match = 0x00B4E730
    mem = FakeScanMem([match], {})
    assert find_function(mem, d) == match


def test_walkthrough_spec_hooks_after_the_call():
    # walkthrough hooks the lea just PAST the call: func = match - (-5) = match + 5, verify 8D B8.
    w = HOOKS["walkthrough"]
    assert w.prologue_back == -5 and w.stolen_len == 6
    assert w.wrap_width == 31 and w.lines_per_page == 0 and w.sync is True
    assert w.pattern is not None  # matched via wildcard pattern, not a literal signature
    match = 0x006E13C8
    func = match + 5
    mem = FakeScanMem([match], {func: b"\x8d\xb8"})  # lea edi,[eax+disp32]
    assert find_function(mem, w) == func


def test_walkthrough_spec_rejects_wrong_post_call_opcode():
    w = HOOKS["walkthrough"]
    match = 0x006E13C8
    mem = FakeScanMem([match], {match + 5: b"\x90\x90"})  # not the lea
    assert find_function(mem, w) is None


def test_find_function_escapes_literal_signature_when_no_pattern():
    # A spec without a wildcard `pattern` matches its signature literally (re.escaped). The escaping
    # guard matters because signature bytes can be regex metacharacters (e.g. 0x5B '[', 0x5E '^').
    spec = _spec(signature=b"\x5b\x5e\x88", pattern=None, prologue_back=0, prologue_verify=b"")
    match = 0x00400000
    assert find_function(FakeScanMem([match], {}), spec) == match


def test_registry_quest_fields():
    q = HOOKS["quest"]
    assert [off for off, _ in q.fields] == [20, 76, 132, 640, 744] and q.prologue_back == 0x115
    assert all(size > 0 for _, size in q.fields)  # every field has a bounded slot
