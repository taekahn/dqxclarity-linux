"""Tests for the provider factory and the claude_cli shell-out provider.

ALL subprocess/network access is mocked: shutil.which is faked so no real ``claude`` binary is
required, and subprocess.run is replaced with canned stdout/return codes. The factory tests build
provider instances but never call ``.translate()`` on the network-backed Google provider.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from dqxclarity.translate.providers import get_provider
from dqxclarity.translate.providers import claude_cli as claude_mod
from dqxclarity.translate.providers.claude_cli import ClaudeCliProvider, _SYSTEM


# =========================================================================== factory: get_provider


@pytest.mark.parametrize("name", ["", "none"])
def test_factory_none_and_empty_return_none(name):
    # "none"/"" select pure-local mode -> no provider object at all.
    assert get_provider(name) is None


def test_factory_claude_cli(monkeypatch):
    # Avoid touching the real PATH during construction.
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    p = get_provider("claude_cli", model="haiku")
    assert isinstance(p, ClaudeCliProvider)
    assert p.name == "claude_cli"
    assert p.model == "haiku"


@pytest.mark.parametrize("name", ["googletranslatefree", "google"])
def test_factory_google_and_alias(name):
    p = get_provider(name)
    # Both the canonical name and the "google" alias build the free Google provider.
    assert type(p).__name__ == "GoogleTranslateFreeProvider"
    assert p.name == "googletranslatefree"


def test_factory_unknown_raises_valueerror():
    with pytest.raises(ValueError, match="unknown translation provider"):
        get_provider("totally-made-up")


def test_factory_unknown_includes_name_in_message():
    with pytest.raises(ValueError) as ei:
        get_provider("xyz")
    assert "xyz" in str(ei.value)


# =========================================================================== claude_cli construction


def test_claude_available_reflects_binary_presence(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    assert ClaudeCliProvider().available() is True
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: None)
    assert ClaudeCliProvider().available() is False


# =========================================================================== claude_cli.translate()


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _envelope(arr: list) -> str:
    """Mimic `claude -p --output-format json`: a JSON object whose `result` is the model's text."""
    return json.dumps({"type": "result", "result": json.dumps(arr, ensure_ascii=False)})


def test_translate_empty_input_short_circuits(monkeypatch):
    # No binary lookup, no subprocess for an empty batch.
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    called = []
    monkeypatch.setattr(claude_mod.subprocess, "run", lambda *a, **k: called.append(1))
    assert ClaudeCliProvider().translate([]) == []
    assert called == []


def test_translate_no_binary_returns_all_none(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: None)
    called = []
    monkeypatch.setattr(claude_mod.subprocess, "run", lambda *a, **k: called.append(1))
    out = ClaudeCliProvider().translate(["a", "b", "c"])
    assert out == [None, None, None]
    assert called == []  # never shelled out


def test_translate_builds_expected_cli_invocation(monkeypatch):
    """Asserts the exact argv: claude -p <prompt> --output-format json (+ --model when set),
    and that the prompt carries the system instruction and the input JSON array verbatim."""
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    captured = {}

    def fake_run(cmd, *, capture_output, text, timeout, check):
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["timeout"] = timeout
        captured["check"] = check
        return _FakeProc(stdout=_envelope(["Hello", "Thanks"]))

    monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)

    texts = ["こんにちは", "ありがとう"]
    out = ClaudeCliProvider(model="haiku", timeout=99.0).translate(texts)
    assert out == ["Hello", "Thanks"]

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/claude"
    assert cmd[1] == "-p"
    prompt = cmd[2]
    assert prompt.startswith(_SYSTEM)
    assert json.dumps(texts, ensure_ascii=False) in prompt  # input array embedded verbatim
    assert cmd[3:5] == ["--output-format", "json"]
    assert cmd[5:] == ["--model", "haiku"]  # model appended only when set
    # run() invoked with the safe, non-raising options.
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["timeout"] == 99.0
    assert captured["check"] is False


def test_translate_omits_model_flag_when_unset(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(stdout=_envelope(["X"]))

    monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)
    assert ClaudeCliProvider(model="").translate(["a"]) == ["X"]
    assert "--model" not in captured["cmd"]


def test_translate_parses_canned_envelope(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout=_envelope(["One", "Two", "Three"])),
    )
    assert ClaudeCliProvider().translate(["a", "b", "c"]) == ["One", "Two", "Three"]


def test_translate_nonzero_exit_returns_all_none(monkeypatch):
    """A non-zero CLI exit (throttled, auth error, etc.) yields None per item — never raises."""
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout="boom", returncode=1),
    )
    out = ClaudeCliProvider().translate(["a", "b"])
    assert out == [None, None]


def test_translate_timeout_handled_without_crashing(monkeypatch):
    """A subprocess timeout is caught and degraded to None per item (gameplay never blocks)."""
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

    monkeypatch.setattr(claude_mod.subprocess, "run", boom)
    out = ClaudeCliProvider().translate(["a", "b", "c"])
    assert out == [None, None, None]


def test_translate_oserror_handled_without_crashing(monkeypatch):
    """An OSError launching the binary (e.g. it vanished) is caught and degraded to None per item."""
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(claude_mod.subprocess, "run", boom)
    assert ClaudeCliProvider().translate(["a"]) == [None]


def test_translate_length_mismatch_returns_all_none(monkeypatch):
    # Model returned the wrong array length -> the whole batch is rejected (None per item).
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout=_envelope(["only-one"])),
    )
    assert ClaudeCliProvider().translate(["a", "b"]) == [None, None]


# =========================================================================== claude_cli._parse()


def test_parse_bare_array_without_envelope():
    # If the CLI already handed us the bare result (no JSON envelope), parse it directly.
    assert ClaudeCliProvider._parse('["Hi", "Bye"]', 2) == ["Hi", "Bye"]


def test_parse_envelope_result():
    env = json.dumps({"type": "result", "result": '["Hello", "World"]'})
    assert ClaudeCliProvider._parse(env, 2) == ["Hello", "World"]


def test_parse_json_fenced_array():
    fenced = json.dumps({"result": "```json\n[\"A\", \"B\"]\n```"})
    assert ClaudeCliProvider._parse(fenced, 2) == ["A", "B"]


def test_parse_array_with_surrounding_prose():
    # Leading/trailing commentary is tolerated by slicing between the first [ and last ].
    env = json.dumps({"result": "Sure, here you go: [\"X\", \"Y\"] -- done."})
    assert ClaudeCliProvider._parse(env, 2) == ["X", "Y"]


def test_parse_null_entries_preserved_as_none():
    env = json.dumps({"result": '["A", null, "C"]'})
    assert ClaudeCliProvider._parse(env, 3) == ["A", None, "C"]


def test_parse_non_string_entries_stringified():
    env = json.dumps({"result": "[1, 2.5, true]"})
    assert ClaudeCliProvider._parse(env, 3) == ["1", "2.5", "True"]


def test_parse_garbage_returns_all_none():
    assert ClaudeCliProvider._parse("not json at all", 2) == [None, None]


def test_parse_non_array_result_returns_all_none():
    # Result is valid JSON but not a list (e.g. an object) -> rejected.
    env = json.dumps({"result": '{"a": 1}'})
    assert ClaudeCliProvider._parse(env, 1) == [None]


def test_parse_length_mismatch_returns_all_none():
    env = json.dumps({"result": '["a", "b", "c"]'})
    assert ClaudeCliProvider._parse(env, 2) == [None, None]


# ===================================================================== claude_cli.translate_rich()


from dqxclarity.translate.providers.claude_cli import _SYSTEM_RICH


def _items(n: int) -> list[dict]:
    return [
        {"ja": f"ja{i}", "glossary": {}, "names": {}, "baseline": None, "surface": None}
        for i in range(n)
    ]


def test_translate_rich_builds_expected_invocation(monkeypatch):
    """The rich path embeds _SYSTEM_RICH + the items JSON, uses -p/--output-format json (+ --model)."""
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(stdout=_envelope(["A", "B"]))

    monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)

    items = _items(2)
    out = ClaudeCliProvider(model="haiku").translate_rich(items)
    assert out == ["A", "B"]

    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    prompt = cmd[2]
    assert _SYSTEM_RICH in prompt
    assert json.dumps(items, ensure_ascii=False) in prompt  # items embedded verbatim
    assert cmd[3:5] == ["--output-format", "json"]
    assert cmd[5:] == ["--model", "haiku"]


def test_translate_rich_parses_array_of_strings(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess, "run", lambda *a, **k: _FakeProc(stdout=_envelope(["One", "Two"]))
    )
    assert ClaudeCliProvider().translate_rich(_items(2)) == ["One", "Two"]


def test_translate_rich_parses_array_of_en_objects(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout=_envelope([{"en": "One"}, {"en": "Two"}])),
    )
    assert ClaudeCliProvider().translate_rich(_items(2)) == ["One", "Two"]


def test_translate_rich_length_mismatch_returns_all_none(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess, "run", lambda *a, **k: _FakeProc(stdout=_envelope(["only-one"]))
    )
    assert ClaudeCliProvider().translate_rich(_items(2)) == [None, None]


def test_translate_rich_dict_without_en_rejects_batch(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout=_envelope([{"text": "One"}, {"en": "Two"}])),
    )
    assert ClaudeCliProvider().translate_rich(_items(2)) == [None, None]


def test_translate_rich_empty_input_short_circuits(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    called = []
    monkeypatch.setattr(claude_mod.subprocess, "run", lambda *a, **k: called.append(1))
    assert ClaudeCliProvider().translate_rich([]) == []
    assert called == []


def test_translate_rich_no_binary_returns_all_none(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: None)
    called = []
    monkeypatch.setattr(claude_mod.subprocess, "run", lambda *a, **k: called.append(1))
    assert ClaudeCliProvider().translate_rich(_items(2)) == [None, None]
    assert called == []


def test_translate_rich_nonzero_exit_returns_all_none(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_mod.subprocess, "run", lambda *a, **k: _FakeProc(stdout="boom", returncode=1)
    )
    assert ClaudeCliProvider().translate_rich(_items(2)) == [None, None]


def test_translate_rich_timeout_returns_all_none(monkeypatch):
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

    monkeypatch.setattr(claude_mod.subprocess, "run", boom)
    assert ClaudeCliProvider().translate_rich(_items(3)) == [None, None, None]


def test_translate_rich_oserror_returns_all_none(monkeypatch):
    """Parity with translate(): an OSError launching the binary degrades to None per item."""
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(claude_mod.subprocess, "run", boom)
    assert ClaudeCliProvider().translate_rich(_items(2)) == [None, None]


# ======================================================================== claude_cli._parse_rich()


def test_parse_rich_string_array():
    assert ClaudeCliProvider._parse_rich(_envelope(["Hi", "Bye"]), 2) == ["Hi", "Bye"]


def test_parse_rich_bare_array_without_envelope():
    # Parity with _parse: a bare result (no JSON envelope) parses directly.
    assert ClaudeCliProvider._parse_rich('["Hi", "Bye"]', 2) == ["Hi", "Bye"]


def test_parse_rich_object_array():
    env = _envelope([{"en": "Hi"}, {"en": "Bye"}])
    assert ClaudeCliProvider._parse_rich(env, 2) == ["Hi", "Bye"]


def test_parse_rich_json_fenced():
    fenced = json.dumps({"result": '```json\n["A", "B"]\n```'})
    assert ClaudeCliProvider._parse_rich(fenced, 2) == ["A", "B"]


def test_parse_rich_prose_wrapped():
    env = json.dumps({"result": 'Here you go: ["X", "Y"] enjoy'})
    assert ClaudeCliProvider._parse_rich(env, 2) == ["X", "Y"]


def test_parse_rich_length_mismatch():
    assert ClaudeCliProvider._parse_rich(_envelope(["a", "b", "c"]), 2) == [None, None]


def test_parse_rich_garbage_returns_all_none():
    assert ClaudeCliProvider._parse_rich("not json at all", 2) == [None, None]


def test_parse_rich_null_entries_preserved():
    env = _envelope(["A", None, "C"])
    assert ClaudeCliProvider._parse_rich(env, 3) == ["A", None, "C"]


def test_parse_rich_other_type_rejects_batch():
    # A bare number/list element is neither str/None/{"en":...} -> reject the whole batch.
    env = _envelope(["A", 5])
    assert ClaudeCliProvider._parse_rich(env, 2) == [None, None]


# --- GoogleTranslateFreeProvider: transient-empty retry (added 2026-06-15) -------------------- #


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


def test_google_retries_transient_missing_result_container(monkeypatch):
    """The free endpoint flakes (no result-container) on a request that succeeds moments later —
    especially on glossified mixed JA+EN strings. A retry must turn that transient empty into a hit."""
    from dqxclarity.translate.providers import googletranslatefree as g

    p = g.GoogleTranslateFreeProvider()
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)  # don't actually back off in tests
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp("<html>flaked: no result container</html>")  # transient empty
        return _FakeResp('<div class="result-container">Hello.</div>')

    monkeypatch.setattr(p._client, "get", fake_get)
    assert p.translate(["こんにちは"]) == ["Hello."]
    assert calls["n"] == 2  # retried exactly once before succeeding


def test_google_gives_up_after_bounded_attempts(monkeypatch):
    """A genuine outage (always empty) fails fast after _ATTEMPTS — caller leaves text Japanese."""
    from dqxclarity.translate.providers import googletranslatefree as g

    p = g.GoogleTranslateFreeProvider()
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return _FakeResp("<html>no container</html>")

    monkeypatch.setattr(p._client, "get", fake_get)
    assert p.translate(["x"]) == [None]
    assert calls["n"] == g._ATTEMPTS  # bounded — didn't loop forever


def test_google_no_retry_on_real_empty_result(monkeypatch):
    """An empty result-container is a REAL answer (not a flake) -> return None WITHOUT retrying."""
    from dqxclarity.translate.providers import googletranslatefree as g

    p = g.GoogleTranslateFreeProvider()
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return _FakeResp('<div class="result-container"></div>')  # found, but empty

    monkeypatch.setattr(p._client, "get", fake_get)
    assert p.translate(["x"]) == [None]
    assert calls["n"] == 1  # found the container first try -> no retry


# ===================================================================== ClaudeApiProvider (HTTP API)
#
# The HTTP-API sibling of claude_cli: same prompts, same parsers, same fail-safe contract — only the
# transport differs (httpx POST vs subprocess). ALL network access is mocked: ANTHROPIC_API_KEY is
# monkeypatched into os.environ, and the provider's httpx _client.post is replaced with a canned
# response so no real key or request ever leaves the test.

from dqxclarity.translate.providers import claude_api as api_mod
from dqxclarity.translate.providers.claude_api import ClaudeApiProvider
from dqxclarity.translate.providers.claude_cli import _SYSTEM_RICH


class _FakeApiResp:
    """Minimal stand-in for an httpx.Response: a status_code + a .json() body."""

    def __init__(self, body: object, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _api_msg(text: str) -> dict:
    """Mimic an Anthropic Messages API success body: content is a list of typed blocks."""
    return {"content": [{"type": "text", "text": text}]}


def _set_key(monkeypatch, value: str = "sk-test-key") -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", value)


def test_api_available_reflects_key_presence(monkeypatch):
    _set_key(monkeypatch)
    assert ClaudeApiProvider().available() is True
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ClaudeApiProvider().available() is False


def test_api_default_model_is_sonnet(monkeypatch):
    _set_key(monkeypatch)
    assert ClaudeApiProvider().model == "claude-sonnet-4-6"
    assert ClaudeApiProvider(model="haiku").model == "haiku"


def test_api_translate_empty_input_short_circuits(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    called = []
    monkeypatch.setattr(p._client, "post", lambda *a, **k: called.append(1))
    assert p.translate([]) == []
    assert called == []  # never hit the network for an empty batch


def test_api_translate_no_key_returns_all_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = ClaudeApiProvider()
    called = []
    monkeypatch.setattr(p._client, "post", lambda *a, **k: called.append(1))
    out = p.translate(["a", "b", "c"])
    assert out == [None, None, None]
    assert called == []  # never made an HTTP call without a key


def test_api_translate_builds_expected_request(monkeypatch):
    """Asserts URL + headers (x-api-key/anthropic-version/content-type) + body (model, system,
    user content embedding the input JSON verbatim) and that a canned response parses correctly."""
    _set_key(monkeypatch, "sk-abc123")
    p = ClaudeApiProvider(model="haiku")
    captured = {}

    def fake_post(url, *, headers, json):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeApiResp(_api_msg('["Hello", "Thanks"]'))

    monkeypatch.setattr(p._client, "post", fake_post)

    texts = ["こんにちは", "ありがとう"]
    out = p.translate(texts)
    assert out == ["Hello", "Thanks"]

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    h = captured["headers"]
    assert h["x-api-key"] == "sk-abc123"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["content-type"] == "application/json"
    body = captured["json"]
    assert body["model"] == "haiku"
    assert body["system"] == _SYSTEM
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] > 0
    user = body["messages"][0]
    assert user["role"] == "user"
    assert json.dumps(texts, ensure_ascii=False) in user["content"]  # input embedded verbatim


def test_api_translate_default_model_in_body(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()  # no model override
    captured = {}

    def fake_post(url, *, headers, json):
        captured["json"] = json
        return _FakeApiResp(_api_msg('["X"]'))

    monkeypatch.setattr(p._client, "post", fake_post)
    assert p.translate(["a"]) == ["X"]
    assert captured["json"]["model"] == "claude-sonnet-4-6"  # default Sonnet


def test_api_translate_parses_canned_response(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client, "post", lambda *a, **k: _FakeApiResp(_api_msg('["A", "B"]'))
    )
    assert p.translate(["x", "y"]) == ["A", "B"]


def test_api_translate_non_200_returns_all_none(monkeypatch):
    """A non-2xx status (auth error, rate limit, etc.) yields None per item — never raises."""
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client, "post", lambda *a, **k: _FakeApiResp({"error": "nope"}, status_code=429)
    )
    assert p.translate(["a", "b"]) == [None, None]


def test_api_translate_httpx_error_returns_all_none(monkeypatch):
    """A transport-level httpx error is caught and degraded to None per item (never blocks)."""
    _set_key(monkeypatch)
    p = ClaudeApiProvider()

    def boom(*a, **k):
        raise api_mod.httpx.ConnectError("network down")

    monkeypatch.setattr(p._client, "post", boom)
    assert p.translate(["a", "b", "c"]) == [None, None, None]


def test_api_translate_malformed_content_returns_all_none(monkeypatch):
    """A 200 with no usable text block (e.g. missing 'content') degrades to None per item."""
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(p._client, "post", lambda *a, **k: _FakeApiResp({"nope": 1}))
    assert p.translate(["a", "b"]) == [None, None]


def test_api_translate_empty_content_list_returns_all_none(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(p._client, "post", lambda *a, **k: _FakeApiResp({"content": []}))
    assert p.translate(["a", "b"]) == [None, None]


def test_api_translate_length_mismatch_returns_all_none(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client, "post", lambda *a, **k: _FakeApiResp(_api_msg('["only-one"]'))
    )
    assert p.translate(["a", "b"]) == [None, None]


def test_api_translate_skips_non_text_blocks(monkeypatch):
    """The parser reads the FIRST type=='text' block, ignoring leading non-text blocks."""
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    body = {"content": [{"type": "thinking", "text": "hmm"}, {"type": "text", "text": '["A"]'}]}
    monkeypatch.setattr(p._client, "post", lambda *a, **k: _FakeApiResp(body))
    assert p.translate(["x"]) == ["A"]


# --- ClaudeApiProvider.translate_rich() --------------------------------------------------------- #


def test_api_translate_rich_empty_input_short_circuits(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    called = []
    monkeypatch.setattr(p._client, "post", lambda *a, **k: called.append(1))
    assert p.translate_rich([]) == []
    assert called == []


def test_api_translate_rich_no_key_returns_all_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = ClaudeApiProvider()
    called = []
    monkeypatch.setattr(p._client, "post", lambda *a, **k: called.append(1))
    assert p.translate_rich(_items(2)) == [None, None]
    assert called == []


def test_api_translate_rich_builds_expected_request(monkeypatch):
    """The rich path uses _SYSTEM_RICH and embeds the items JSON verbatim in the user content."""
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    captured = {}

    def fake_post(url, *, headers, json):
        captured["json"] = json
        return _FakeApiResp(_api_msg('["A", "B"]'))

    monkeypatch.setattr(p._client, "post", fake_post)

    items = _items(2)
    assert p.translate_rich(items) == ["A", "B"]
    body = captured["json"]
    assert body["system"] == _SYSTEM_RICH
    assert json.dumps(items, ensure_ascii=False) in body["messages"][0]["content"]


def test_api_translate_rich_parses_array_of_strings(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client, "post", lambda *a, **k: _FakeApiResp(_api_msg('["One", "Two"]'))
    )
    assert p.translate_rich(_items(2)) == ["One", "Two"]


def test_api_translate_rich_parses_array_of_en_objects(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client,
        "post",
        lambda *a, **k: _FakeApiResp(_api_msg('[{"en": "One"}, {"en": "Two"}]')),
    )
    assert p.translate_rich(_items(2)) == ["One", "Two"]


def test_api_translate_rich_non_200_returns_all_none(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client, "post", lambda *a, **k: _FakeApiResp({"error": "x"}, status_code=500)
    )
    assert p.translate_rich(_items(2)) == [None, None]


def test_api_translate_rich_httpx_error_returns_all_none(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()

    def boom(*a, **k):
        raise api_mod.httpx.ReadTimeout("slow")

    monkeypatch.setattr(p._client, "post", boom)
    assert p.translate_rich(_items(3)) == [None, None, None]


def test_api_translate_rich_length_mismatch_returns_all_none(monkeypatch):
    _set_key(monkeypatch)
    p = ClaudeApiProvider()
    monkeypatch.setattr(
        p._client, "post", lambda *a, **k: _FakeApiResp(_api_msg('["only-one"]'))
    )
    assert p.translate_rich(_items(2)) == [None, None]


# ====================================================================== resolver: get_provider("claude")
#
# The single auto-resolver: prefer the HTTP API when a key is set, else the CLI when the binary is on
# PATH, else None (with a RuntimeWarning). shutil.which is patched in the providers package namespace
# (where the resolver imports it) so the real PATH is never consulted.

from dqxclarity.translate import providers as providers_pkg


def test_resolver_claude_key_and_cli_builds_api_with_cli_fallback(monkeypatch):
    from dqxclarity.translate.providers._claude_fallback import ClaudeFallbackProvider

    _set_key(monkeypatch)
    # Key AND CLI present -> the composite: API primary, CLI fallback per item.
    monkeypatch.setattr(providers_pkg.shutil, "which", lambda _: "/usr/bin/claude")
    p = get_provider("claude")
    assert isinstance(p, ClaudeFallbackProvider)
    assert isinstance(p.primary, ClaudeApiProvider)
    assert isinstance(p.fallback, ClaudeCliProvider)
    assert p.name == "claude_api"            # rank-2 label so a finished line isn't re-queued


def test_resolver_claude_key_only_is_api_directly(monkeypatch):
    _set_key(monkeypatch)
    monkeypatch.setattr(providers_pkg.shutil, "which", lambda _: None)  # no CLI
    p = get_provider("claude")
    assert isinstance(p, ClaudeApiProvider)  # no fallback available -> bare API
    assert p.name == "claude_api"


def test_resolver_claude_falls_back_to_cli_when_no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(providers_pkg.shutil, "which", lambda _: "/usr/bin/claude")
    p = get_provider("claude")
    assert isinstance(p, ClaudeCliProvider)
    assert p.name == "claude_cli"


def test_resolver_claude_none_and_warns_when_neither(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(providers_pkg.shutil, "which", lambda _: None)
    with pytest.warns(RuntimeWarning):
        p = get_provider("claude")
    assert p is None


def test_resolver_claude_passes_kwargs_to_api(monkeypatch):
    _set_key(monkeypatch)
    monkeypatch.setattr(providers_pkg.shutil, "which", lambda _: None)
    p = get_provider("claude", model="haiku")
    assert isinstance(p, ClaudeApiProvider)
    assert p.model == "haiku"


def test_factory_claude_api_explicit(monkeypatch):
    _set_key(monkeypatch)
    p = get_provider("claude_api", model="haiku")
    assert isinstance(p, ClaudeApiProvider)
    assert p.name == "claude_api"
    assert p.model == "haiku"


def test_api_provider_name_is_rank_2():
    # The bare API provider must also be a rank-2 source (else finished lines re-queue forever).
    from dqxclarity.translate.db import rank_of

    assert ClaudeApiProvider.name == "claude_api"
    assert rank_of(ClaudeApiProvider.name) == 2


# =============================================================== ClaudeFallbackProvider (api->cli)
#
# Per-ITEM fallback: any None the primary (API) returns — whether the whole call errored (all None)
# or the model dropped a few — is re-sent to the fallback (CLI) and merged back. An item both fail
# stays None. The composite is rank-2 ("claude_api") so a finished line isn't re-queued forever.

from dqxclarity.translate.providers._claude_fallback import ClaudeFallbackProvider
from dqxclarity.translate.db import rank_of


class _RecordingProvider:
    """A stub provider with scripted return values; records the batches it was asked to translate."""

    def __init__(self, name, results_for):
        self.name = name
        self._results_for = results_for       # callable: batch -> list[str|None]
        self.seen = []                         # batches it was called with

    def available(self):
        return True

    def translate(self, batch):
        self.seen.append(list(batch))
        return self._results_for(batch)

    translate_rich = translate                # same scripted behavior for both channels


def test_fallback_uses_primary_when_it_succeeds():
    primary = _RecordingProvider("claude_api", lambda b: ["P"] * len(b))
    fb = _RecordingProvider("claude_cli", lambda b: ["F"] * len(b))
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate(["a", "b"]) == ["P", "P"]
    assert fb.seen == []                       # fallback never consulted when primary fully succeeds


def test_fallback_retries_only_the_missing_items_on_partial():
    # Primary returns one None (model dropped item index 1); only THAT item goes to the fallback.
    primary = _RecordingProvider("claude_api", lambda b: ["P0", None, "P2"])
    fb = _RecordingProvider("claude_cli", lambda b: ["Frescued"])
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate(["a", "b", "c"]) == ["P0", "Frescued", "P2"]
    assert fb.seen == [["b"]]                   # fallback got ONLY the missing item, not the batch


def test_fallback_retries_whole_batch_when_primary_all_none():
    # API call errored -> primary returns all None -> the whole batch is retried via the CLI.
    primary = _RecordingProvider("claude_api", lambda b: [None] * len(b))
    fb = _RecordingProvider("claude_cli", lambda b: ["F"] * len(b))
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate(["a", "b"]) == ["F", "F"]
    assert fb.seen == [["a", "b"]]


def test_fallback_item_stays_none_when_both_fail():
    primary = _RecordingProvider("claude_api", lambda b: [None] * len(b))
    fb = _RecordingProvider("claude_cli", lambda b: [None] * len(b))
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate(["a", "b"]) == [None, None]


def test_fallback_rich_channel_merges_per_item():
    primary = _RecordingProvider("claude_api", lambda b: [None, "P1"])
    fb = _RecordingProvider("claude_cli", lambda b: ["Frescued"])
    comp = ClaudeFallbackProvider(primary, fb)
    items = [{"ja": "x"}, {"ja": "y"}]
    assert comp.translate_rich(items) == ["Frescued", "P1"]
    assert fb.seen == [[{"ja": "x"}]]           # only the missing item, as a dict, to the rich channel


def test_fallback_rich_channel_both_fail_stays_none():
    primary = _RecordingProvider("claude_api", lambda b: [None, None])
    fb = _RecordingProvider("claude_cli", lambda b: [None, None])
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate_rich([{"ja": "x"}, {"ja": "y"}]) == [None, None]


def test_fallback_tolerates_short_fallback_result():
    # Defensive: a fallback that returns fewer items than asked must NOT IndexError; the unfilled
    # missing slots simply stay None.
    primary = _RecordingProvider("claude_api", lambda b: [None, None, None])
    fb = _RecordingProvider("claude_cli", lambda b: ["only-one"])  # short by design
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate(["a", "b", "c"]) == ["only-one", None, None]


def test_fallback_empty_input_short_circuits():
    primary = _RecordingProvider("claude_api", lambda b: ["P"] * len(b))
    fb = _RecordingProvider("claude_cli", lambda b: ["F"] * len(b))
    comp = ClaudeFallbackProvider(primary, fb)
    assert comp.translate([]) == []
    assert primary.seen == [] and fb.seen == []


def test_fallback_name_is_rank_2():
    # Critical: the composite must report a rank-2 source, else the worker re-queues finished lines.
    comp = ClaudeFallbackProvider(
        _RecordingProvider("claude_api", lambda b: b),
        _RecordingProvider("claude_cli", lambda b: b),
    )
    assert comp.name == "claude_api"
    assert rank_of(comp.name) == 2


def test_fallback_available_is_or_of_both():
    class _Off(_RecordingProvider):
        def available(self):
            return False

    comp = ClaudeFallbackProvider(
        _Off("claude_api", lambda b: b), _RecordingProvider("claude_cli", lambda b: b)
    )
    assert comp.available() is True            # CLI still up
    comp_both_off = ClaudeFallbackProvider(
        _Off("claude_api", lambda b: b), _Off("claude_cli", lambda b: b)
    )
    assert comp_both_off.available() is False
