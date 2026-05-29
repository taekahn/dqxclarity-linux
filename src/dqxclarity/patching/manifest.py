"""Patch manifest model.

A manifest describes the translated files to install, organized into toggleable **groups**
that mirror upstream dqxclarity's three patch operations:

  * ``game_files``  — the translated ``data00000000`` archive (menus/UI/system text). This is
    the main user-visible static translation. Applied by default.
  * ``config_exe``  — a translated ``DQXConfig.exe`` (the config tool). Optional.
  * ``launcher_exe``— a translated ``DQXLauncher.exe`` (the boot launcher). Optional.

Keeping this data-driven means a game/translation update only needs a refreshed manifest, not
a code change. Upstream publishes its assets via GitHub release "latest/download" redirects,
which need no API call or auth; we use those URLs directly.

Schema (JSON)::

    {
      "name": "dqx-en",
      "version": "tracks-latest",
      "groups": {
        "game_files": {
          "description": "...",
          "optional": false,
          "files": [{"target": "Game/Content/Data/...", "url": "https://...",
                     "sha256": "", "size": 0}]
        }
      }
    }

``target`` is always relative to the install root (the "DRAGON QUEST X" directory).
``sha256``/``size`` are optional (upstream publishes none); when present they are verified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Bundled fallback manifest shipped with the package.
DEFAULT_MANIFEST = Path(__file__).with_name("data") / "default_manifest.json"

# Groups applied by default when the user doesn't ask for specific ones.
DEFAULT_GROUPS = ("game_files",)


@dataclass
class PatchFile:
    target: str  # path relative to install_root
    url: str
    sha256: str = ""
    size: int = 0

    def __post_init__(self) -> None:
        p = Path(self.target)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"unsafe manifest target: {self.target!r}")


@dataclass
class PatchGroup:
    name: str
    description: str = ""
    optional: bool = False
    files: list[PatchFile] = field(default_factory=list)


@dataclass
class Manifest:
    name: str
    version: str
    groups: dict[str, PatchGroup]

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        groups: dict[str, PatchGroup] = {}
        for gname, g in d.get("groups", {}).items():
            groups[gname] = PatchGroup(
                name=gname,
                description=g.get("description", ""),
                optional=g.get("optional", False),
                files=[PatchFile(**f) for f in g.get("files", [])],
            )
        return cls(name=d.get("name", "unknown"), version=d.get("version", "0"), groups=groups)

    def resolve_groups(self, requested: set[str] | None) -> list[PatchGroup]:
        """Return the groups to act on.

        ``requested`` None → default groups; otherwise the named groups (validated).
        """
        if requested is None:
            names = [g for g in DEFAULT_GROUPS if g in self.groups]
        else:
            unknown = requested - set(self.groups)
            if unknown:
                raise ValueError(f"unknown patch group(s): {', '.join(sorted(unknown))}")
            names = [g for g in self.groups if g in requested]
        return [self.groups[n] for n in names]

    def has_files(self) -> bool:
        return any(g.files for g in self.groups.values())


def load_manifest(source: str | None) -> Manifest:
    """Load a manifest from a URL, a local path, or the bundled default (``source`` empty)."""
    if not source:
        text = DEFAULT_MANIFEST.read_text(encoding="utf-8")
    elif source.startswith(("http://", "https://")):
        resp = httpx.get(source, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
    else:
        text = Path(source).expanduser().read_text(encoding="utf-8")
    return Manifest.from_dict(json.loads(text))
