"""User configuration (TOML) stored in the platform config dir.

Read with stdlib ``tomllib``; written with a tiny serializer covering our known schema (the
stdlib has no TOML writer and we don't want a hard dependency just for this).
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TypeVar

from platformdirs import user_config_path

CONFIG_DIR = user_config_path("dqxclarity")
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class TranslateConfig:
    # Fast, synchronous MT for instant first-view dialogue (community DB still takes priority).
    # "none" = pure-local; "googletranslatefree" = free Google (no key, ~200ms).
    provider: str = "none"
    # Slow, higher-quality MT run in the background to UPGRADE cache entries (so a re-viewed line
    # shows the better translation). "" = off; "claude" = auto-resolve (prefers the metered HTTP API
    # when ANTHROPIC_API_KEY is set, else the "claude" CLI subscription); "claude_cli"/"claude_api"
    # force one transport explicitly.
    upgrade_provider: str = ""
    batch_size: int = 16  # strings per claude_cli invocation (amortizes CLI startup)
    # Optional model override: the `claude -p --model` flag for the CLI (short aliases like "haiku"
    # are fine) AND the model id for claude_api (the HTTP API needs a FULL id, e.g.
    # "claude-haiku-4-5", not "haiku"). "" = each provider's default (claude_api defaults to Sonnet).
    claude_model: str = ""
    romanize_names: bool = True  # romanize player/NPC names locally via pykakasi
    wrap_width: int = 46  # dialogue line wrap width (chars); tune to the in-game box
    lines_per_page: int = 3  # lines per <br> page break; tune to the in-game box height
    # Player/sibling names for community-DB placeholder matching (<pnplacehold>/<snplacehold>).
    player_name_ja: str = ""  # e.g. "タイカン"
    player_name_en: str = ""  # e.g. "Taikan"
    sibling_name_ja: str = ""
    sibling_name_en: str = ""
    # NOVEL (no upstream equivalent): name-ify Japanese monster/actor names that arrive in the
    # network_text battle-message surface (categories containing <%sB_ACTOR>/<%sB_TARGET>/...). On by
    # default; set False for exact upstream behaviour (battle name tags stay untranslated/dropped).
    battle_names: bool = True
    # NOVEL (no upstream equivalent): "translate the rest" model for the network_text surface. When
    # True (default), build_network_translate_fn DROPS the redundant whitelist (NET_TRANSLATE_
    # CATEGORIES): noise categories are already filtered by is_japanese(ja), name-bearing categories
    # route to the instant name-ify pass, NET_IGNORE stays dropped, and every OTHER Japanese category
    # (community-board post titles, items, unknown prose) flows to the ASYNC text path instead of
    # being silently left Japanese. (The startup "Important Notice" is NOT a network_text category —
    # it's a static memory buffer handled by the notice scanner, runtime/notice_loop.py.) Set False
    # for the exact prior whitelist
    # behaviour (only the 28 NET_TRANSLATE_CATEGORIES are ever touched).
    network_translate_all: bool = True
    # Auto-refresh the translation DB on `run` startup when it's STALE (or never synced). The check
    # is purely LOCAL (a `last_sync` marker) so a fresh DB adds zero startup cost; only a stale DB
    # triggers a one-time network sync. `run --no-sync` overrides this. Mirrors patch.auto_apply.
    auto_sync: bool = True
    sync_max_age_days: int = 7  # consider the DB stale after this many days since the last sync


@dataclass
class PatchConfig:
    # URL of a JSON manifest listing files to patch (see patching.manifest). Empty => use the
    # bundled default manifest shipped with the package.
    manifest_url: str = ""
    patch_config_exe: bool = False  # also patch DQXConfig.exe (translated config UI)
    patch_launcher_exe: bool = False  # also patch DQXLauncher.exe (translated boot launcher)
    # Auto-reapply static file patches when `run` starts and the game is NOT yet running (it's
    # unsafe to patch a running game's mmap'd files). The `run --no-patch` flag overrides this.
    auto_apply: bool = True


@dataclass
class Config:
    install_root: str = ""  # override for auto-discovery (set when game isn't running)
    backup_dir: str = str(CONFIG_DIR / "backups")
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    patch: PatchConfig = field(default_factory=PatchConfig)


_T = TypeVar("_T")


def _coerce(value: object, type_name: str) -> object:
    """Coerce a config value to its declared field type (type names are strings under
    `from __future__ import annotations`). Tolerates values saved as strings by older versions."""
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if type_name == "int":
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
    if type_name == "float":
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
    return str(value)


def _build(cls: type[_T], data: dict) -> _T:
    """Instantiate a dataclass from a dict, ignoring unknown keys and coercing field types."""
    return cls(**{f.name: _coerce(data[f.name], f.type) for f in fields(cls) if f.name in data})


def load() -> Config:
    if not CONFIG_FILE.is_file():
        return Config()
    data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return Config(
        install_root=data.get("install_root", ""),
        backup_dir=data.get("backup_dir", str(CONFIG_DIR / "backups")),
        translate=_build(TranslateConfig, data.get("translate", {})),
        patch=_build(PatchConfig, data.get("patch", {})),
    )


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(cfg: Config) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    d = asdict(cfg)
    lines: list[str] = []
    for key in ("install_root", "backup_dir"):
        lines.append(f"{key} = {_toml_value(d[key])}")
    for table in ("translate", "patch"):
        lines.append("")
        lines.append(f"[{table}]")
        for k, v in d[table].items():
            lines.append(f"{k} = {_toml_value(v)}")
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return CONFIG_FILE
