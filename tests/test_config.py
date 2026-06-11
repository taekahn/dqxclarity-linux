"""Tests for user configuration (TOML load/save round-trip, _coerce, config set).

All filesystem I/O is redirected to ``tmp_path`` by monkeypatching the module-level
``CONFIG_DIR``/``CONFIG_FILE`` globals that ``load``/``save`` read. No network, no game process.
"""

from __future__ import annotations

import pytest

from dqxclarity import config as cfg_mod
from dqxclarity.config import Config, PatchConfig, TranslateConfig, _coerce


@pytest.fixture()
def cfg_files(tmp_path, monkeypatch):
    """Point config.load/save at a throwaway dir under tmp_path."""
    d = tmp_path / "cfgdir"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", d)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", d / "config.toml")
    return d


# --------------------------------------------------------------------------- load() defaults


def test_load_returns_defaults_when_no_file(cfg_files):
    # No config.toml on disk -> a fully-defaulted Config (never raises, never creates the file).
    assert not (cfg_files / "config.toml").exists()
    c = cfg_mod.load()
    assert isinstance(c, Config)
    assert c.install_root == ""
    assert isinstance(c.translate, TranslateConfig)
    assert isinstance(c.patch, PatchConfig)
    assert c.translate.provider == "none"
    assert c.translate.upgrade_provider == ""
    assert c.translate.batch_size == 16
    assert c.translate.romanize_names is True
    assert c.translate.wrap_width == 46
    assert c.translate.lines_per_page == 3
    assert c.patch.patch_config_exe is False
    # load() must not have written a file as a side effect.
    assert not (cfg_files / "config.toml").exists()


# --------------------------------------------------------------------------- save()/load() round-trip


def test_save_then_load_round_trips_every_field(cfg_files):
    """Every scalar field — top-level and nested translate.*/patch.* — survives a save/load cycle
    with its declared type intact (ints stay ints, bools stay bools, unicode stays unicode)."""
    c = Config()
    c.install_root = "/games/dqx"
    c.backup_dir = "/var/backups/dqx"
    c.translate.provider = "googletranslatefree"
    c.translate.upgrade_provider = "claude_cli"
    c.translate.batch_size = 32
    c.translate.claude_model = "haiku"
    c.translate.romanize_names = False
    c.translate.wrap_width = 50
    c.translate.lines_per_page = 0
    c.translate.player_name_ja = "タイカン"
    c.translate.player_name_en = "Taikan"
    c.translate.sibling_name_ja = "シブリング"
    c.translate.sibling_name_en = "Sib"
    c.patch.manifest_url = "https://example.test/manifest.json"
    c.patch.patch_config_exe = True
    c.patch.patch_launcher_exe = True

    path = cfg_mod.save(c)
    assert path == cfg_files / "config.toml"
    assert path.is_file()

    back = cfg_mod.load()
    # Whole-object equality covers every field at once and guards against type drift.
    assert back == c
    # Spot-check the types explicitly (equality alone could be satisfied by stringified values).
    assert isinstance(back.translate.batch_size, int)
    assert isinstance(back.translate.wrap_width, int)
    assert isinstance(back.translate.lines_per_page, int)
    assert isinstance(back.translate.romanize_names, bool)
    assert isinstance(back.patch.patch_config_exe, bool)
    assert back.translate.player_name_ja == "タイカン"  # unicode preserved


def test_save_quotes_strings_with_special_chars(cfg_files):
    """Backslashes and double-quotes in string values are escaped so the TOML stays valid and
    round-trips byte-for-byte (the hand-rolled serializer has no other escaping)."""
    c = Config()
    c.install_root = r'C:\Program Files\"DQX"\game'
    cfg_mod.save(c)
    back = cfg_mod.load()
    assert back.install_root == r'C:\Program Files\"DQX"\game'


def test_load_ignores_unknown_keys_in_file(cfg_files):
    """A config written by a newer/older version with extra keys must load (unknown keys ignored,
    declared keys honoured) rather than crashing the tool."""
    cfg_files.mkdir(parents=True, exist_ok=True)
    (cfg_files / "config.toml").write_text(
        'install_root = "/x"\n'
        "future_top_level = 1\n"
        "\n[translate]\n"
        'provider = "claude_cli"\n'
        "future_nested = true\n"
        "\n[patch]\n"
        "patch_config_exe = true\n",
        encoding="utf-8",
    )
    c = cfg_mod.load()
    assert c.install_root == "/x"
    assert c.translate.provider == "claude_cli"
    assert c.patch.patch_config_exe is True
    # Unknown keys did not leak onto the dataclasses.
    assert not hasattr(c.translate, "future_nested")
    assert not hasattr(c, "future_top_level")


def test_load_tolerates_typed_fields_saved_as_strings(cfg_files):
    """Forward/backward compatibility: a value persisted as a quoted string (older writer) is
    coerced back to its declared type on load — int from "32", bool from "false"."""
    cfg_files.mkdir(parents=True, exist_ok=True)
    (cfg_files / "config.toml").write_text(
        "[translate]\n"
        'batch_size = "32"\n'
        'wrap_width = "60"\n'
        'romanize_names = "false"\n'
        "\n[patch]\n"
        'patch_config_exe = "true"\n',
        encoding="utf-8",
    )
    c = cfg_mod.load()
    assert c.translate.batch_size == 32 and isinstance(c.translate.batch_size, int)
    assert c.translate.wrap_width == 60
    assert c.translate.romanize_names is False
    assert c.patch.patch_config_exe is True


# --------------------------------------------------------------------------- _coerce()


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        (" ON ", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        ("anything-else", False),
    ],
)
def test_coerce_bool(raw, expected):
    assert _coerce(raw, "bool") is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (5, 5),
        ("7", 7),
        (3.0, 3),
        ("-2", -2),
    ],
)
def test_coerce_int_good(raw, expected):
    out = _coerce(raw, "int")
    assert out == expected and isinstance(out, int)


@pytest.mark.parametrize("raw", ["not-a-number", None, "1.5", "", "12px"])
def test_coerce_int_bad_falls_back_to_zero(raw):
    # Bad ints must not raise; they degrade to 0 so a corrupt config never crashes the tool.
    assert _coerce(raw, "int") == 0


@pytest.mark.parametrize("raw", ["nope", None, ""])
def test_coerce_float_bad_falls_back_to_zero(raw):
    assert _coerce(raw, "float") == 0.0


def test_coerce_float_good():
    out = _coerce("1.5", "float")
    assert out == 1.5 and isinstance(out, float)


@pytest.mark.parametrize("raw,expected", [("abc", "abc"), (123, "123"), (True, "True")])
def test_coerce_str(raw, expected):
    # Anything that isn't bool/int/float is stringified (the catch-all branch).
    out = _coerce(raw, "str")
    assert out == expected and isinstance(out, str)


def test_coerce_unknown_type_name_stringifies():
    # An unrecognised declared type falls through to the str branch (defensive default).
    assert _coerce(42, "SomeDataclass") == "42"


# --------------------------------------------------------------------------- config set (CLI fn)


def test_config_set_nested_key_round_trips(cfg_files):
    """Setting a nested key via the CLI's config_set helper persists and reloads correctly,
    including coercion of typed fields written as raw strings."""
    from dqxclarity.cli import config_set

    config_set("translate.provider", "claude_cli")
    config_set("translate.batch_size", "64")
    config_set("patch.manifest_url", "https://example.test/m.json")
    config_set("install_root", "/opt/dqx")

    c = cfg_mod.load()
    assert c.translate.provider == "claude_cli"
    assert c.translate.batch_size == 64 and isinstance(c.translate.batch_size, int)
    assert c.patch.manifest_url == "https://example.test/m.json"
    assert c.install_root == "/opt/dqx"


def test_config_set_unknown_table_raises_exit(cfg_files):
    """An unknown TABLE (the leaf's parent doesn't exist) is rejected with a non-zero exit."""
    import typer

    from dqxclarity.cli import config_set

    with pytest.raises(typer.Exit) as ei:
        config_set("bogus.leaf", "x")
    assert ei.value.exit_code == 1


def test_config_set_unknown_nested_leaf_is_rejected(cfg_files):
    """BUG GUARD: an unknown nested leaf key must be rejected, not silently accepted.

    Current behaviour (FLAGGED in report): because the dataclasses have no __slots__,
    setattr(getattr(cfg, 'translate'), 'nope', 'x') happily creates a bogus attribute, so
    config_set prints "set ..." and exits 0 — but asdict() drops the unknown field on save, so
    NOTHING is persisted. The user is told the value was set when it was silently discarded.
    The correct behaviour is to reject the unknown key with a non-zero exit (like an unknown
    table does). This test asserts the CORRECT behaviour and is expected to FAIL until the
    source bug is fixed by the orchestrator.
    """
    import typer

    from dqxclarity.cli import config_set

    with pytest.raises(typer.Exit) as ei:
        config_set("translate.nope", "x")
    assert ei.value.exit_code == 1
    # And nothing bogus should have been persisted.
    c = cfg_mod.load()
    assert not hasattr(c.translate, "nope")


def test_config_set_unknown_top_level_key_is_rejected(cfg_files):
    """BUG GUARD: an unknown TOP-LEVEL key must be rejected, not silently accepted.

    Same root cause as the nested case: setattr(cfg, 'nope', 'x') succeeds on a slot-less
    dataclass, so config_set reports success while asdict() discards the value on save.
    Asserts the CORRECT (rejecting) behaviour; expected to FAIL until the source is fixed.
    """
    import typer

    from dqxclarity.cli import config_set

    with pytest.raises(typer.Exit) as ei:
        config_set("nope", "x")
    assert ei.value.exit_code == 1


def test_patch_auto_apply_defaults_true_and_round_trips(cfg_files):
    """`run`'s startup auto-patch toggle: default True, and it survives config set/save/load
    (coerced from the raw string the CLI passes) — `config show` reads it straight off load()."""
    from dqxclarity.cli import config_set

    # Default before anyone sets it.
    assert cfg_mod.Config().patch.auto_apply is True

    # Set it off via the CLI helper (value arrives as the raw string "false").
    config_set("patch.auto_apply", "false")
    c = cfg_mod.load()
    assert c.patch.auto_apply is False and isinstance(c.patch.auto_apply, bool)

    # And back on round-trips too.
    config_set("patch.auto_apply", "true")
    assert cfg_mod.load().patch.auto_apply is True


def test_battle_names_defaults_and_round_trip(cfg_files):
    """NOVEL battle-name toggle (#battle_names): defaults True, survives config set/save/load as a
    bool (coerced from the raw string the CLI passes)."""
    from dqxclarity.cli import config_set

    # Default.
    assert TranslateConfig().battle_names is True

    # Toggle off via the CLI helper (value arrives as the raw string "false").
    config_set("translate.battle_names", "false")
    c = cfg_mod.load()
    assert c.translate.battle_names is False and isinstance(c.translate.battle_names, bool)

    # And back on round-trips too.
    config_set("translate.battle_names", "true")
    assert cfg_mod.load().translate.battle_names is True


def test_network_translate_all_defaults_and_round_trip(cfg_files):
    """FEATURE #12 "translate the rest" toggle (#network_translate_all): defaults True, survives
    config set/save/load as a bool (coerced from the raw CLI string)."""
    from dqxclarity.cli import config_set

    # Default.
    assert TranslateConfig().network_translate_all is True

    # Toggle off via the CLI helper (value arrives as the raw string "false").
    config_set("translate.network_translate_all", "false")
    c = cfg_mod.load()
    assert c.translate.network_translate_all is False
    assert isinstance(c.translate.network_translate_all, bool)

    # A full save/load round-trip preserves it, then toggling back on round-trips too.
    cfg_mod.save(c)
    assert cfg_mod.load().translate.network_translate_all is False
    config_set("translate.network_translate_all", "true")
    assert cfg_mod.load().translate.network_translate_all is True


def test_auto_sync_defaults_and_round_trip(cfg_files):
    """`run`'s staleness-gated auto-refresh toggle + threshold (#19): correct defaults, and both
    survive config set/save/load with their declared types (bool / int)."""
    from dqxclarity.cli import config_set

    # Defaults.
    tc = TranslateConfig()
    assert tc.auto_sync is True
    assert tc.sync_max_age_days == 7 and isinstance(tc.sync_max_age_days, int)

    # Toggle off + change the threshold via the CLI helper (values arrive as raw strings).
    config_set("translate.auto_sync", "false")
    config_set("translate.sync_max_age_days", "14")
    c = cfg_mod.load()
    assert c.translate.auto_sync is False and isinstance(c.translate.auto_sync, bool)
    assert c.translate.sync_max_age_days == 14 and isinstance(c.translate.sync_max_age_days, int)

    # And a full save/load round-trip preserves them.
    back = cfg_mod.load()
    cfg_mod.save(back)
    assert cfg_mod.load().translate.auto_sync is False
    assert cfg_mod.load().translate.sync_max_age_days == 14
