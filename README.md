# dqxclarity-linux

A Linux-native, CLI-only fork of [dqxclarity](https://github.com/dqx-translation-project/dqxclarity)
that translates **Dragon Quest X Online** running under Steam + Proton/Wine.

See [`PLAN.md`](PLAN.md) for the full design, decisions, and Phase 0 spike results.

## Status

- **Static file patching** — install detection, manifest-driven download + verify + atomic replace
  with timestamped backups and a game-running safety block.
- **Live translation** — native in-process detour hooks on all surfaces (dialogue, quests,
  walkthrough, corner text, overhead/party names, network text, player names), backed by the curated
  community DB, a ~40k-term proper-noun glossary, and an optional two-tier MT fallback (fast Google
  first-view + background **Claude** upgrade via your subscription CLI or the metered API).

## Install

```sh
python -m venv .venv && . .venv/bin/activate   # fish: source .venv/bin/activate.fish
pip install -e .             # runtime: patch + live-translate
pip install -e '.[dev]'      # + dev tools (pytest, ruff) — needed to run the test suite
```

Live translation uses native in-process hooking (no Frida). Run the tests with `pytest`.

## Run

The everyday flow — detect your install, pull the community translations, then translate live:

```sh
dqxclarity doctor     # preflight: install detection, paths, permissions, deps
dqxclarity sync       # download the curated community DB + proper-noun glossary (run periodically)
dqxclarity run        # live-translate every surface while the game is open (Ctrl-C to stop)
```

`run` attaches to the running game, installs native detour hooks on all surfaces, and writes English
back in place — restoring the game's original bytes on exit. The game must be **open**; launch DQX
once first so your install is auto-detected. Want machine translation for the tail the community DB
doesn't cover (including higher-quality Claude)? See [Live translation](#live-translation).

## Usage

```sh
dqxclarity doctor                 # preflight: install detection, paths, permissions, deps
dqxclarity run                    # live-translate all surfaces (dialogue, quests, names, …) at once
dqxclarity run --hooks dialogue   # or just specific surfaces
dqxclarity sync                   # download the curated community DB + proper-noun glossary
dqxclarity probe                  # inspect the running game + live memory-read smoke test
dqxclarity scan                   # find/read Japanese text in the live game (diagnostic)
dqxclarity patch --dry-run        # show what file patching would change
dqxclarity patch                  # apply translated UI/menu archive (game must be CLOSED)
dqxclarity patch --config --launcher   # also patch DQXConfig.exe + DQXLauncher.exe
dqxclarity restore                # roll back to the most recent backup
dqxclarity romanize "たろう"       # local JP->romaji demo (no game needed)
dqxclarity send-text "ごめんね"     # inject text into the open chat box (Linux IME bypass)
dqxclarity translate-text "…"     # translate one string via the configured provider
dqxclarity config show
dqxclarity config set install_root "/path/to/DRAGON QUEST X"
dqxclarity config set translate.provider googletranslatefree     # fast first-view MT
dqxclarity config set translate.upgrade_provider claude          # background Claude upgrade (API or CLI)
```

## Live translation

Live translation intercepts Japanese the game shows, looks it up, and writes English back. Sources,
in priority order: (1) the **curated community DB** (human translations — the bulk), (2) local
**romaji** for player/NPC names via `pykakasi` (in-process, no service), and (3) an **optional MT
provider** for the leftover untranslated tail. Each unique string is translated once and cached
forever (in-memory dict + SQLite-WAL). A ~40k-term **proper-noun glossary** keeps names, places, and
skills consistent across every MT call.

**Two-tier MT** (both optional, off by default):
- `translate.provider` — a **fast, synchronous** translator for instant first-view (e.g.
  `googletranslatefree`: free Google, no key, ~200ms).
- `translate.upgrade_provider` — a **slow, higher-quality** translator that runs in the **background
  and upgrades** the cache, so a re-viewed line shows the better translation. Set it to **`claude`**
  to auto-resolve the best available transport:
  - the **Anthropic API** when `ANTHROPIC_API_KEY` is set (lightweight, metered) — with per-item
    fallback to the CLI if a call fails;
  - the **Claude Code CLI** (your subscription) when no key is present.

  (`claude_cli` / `claude_api` force one transport.) Claude receives rich context — the glossary as
  canonical pins, your character names, the current draft, and a per-surface register hint — so the
  output reads like proper in-game English. Quality only ever increases (community > claude > google);
  curated human translations are never overwritten.

```sh
dqxclarity config set translate.provider googletranslatefree   # instant first-view
dqxclarity config set translate.upgrade_provider claude         # background quality upgrade
set -x ANTHROPIC_API_KEY sk-ant-...   # optional: route Claude through the metered API (fish syntax)
```

> The API key lives in the `ANTHROPIC_API_KEY` environment variable, **never** in `config.toml`
> (that file is meant to be shareable). The metered cost is tiny — the community DB covers the vast
> majority of text, so only the small uncovered tail ever reaches Claude.

### Dialogue (community DB + name placeholders)

Run `dqxclarity sync` once to pull the curated community dialogue dataset (`merge.xlsx`, ~4k+
human-translated lines) into the cache. Covered dialogue then renders **instantly and correctly
formatted** (it's human-made, with all of DQX's control codes intact). For name-bearing lines,
set your character/sibling names so the `<pnplacehold>`/`<snplacehold>` entries match for you:

```sh
dqxclarity config set translate.player_name_ja "タイカン"
dqxclarity config set translate.player_name_en "Taikan"
dqxclarity run                       # community hit -> as-is; otherwise MT fallback
```

`run`'s dialogue hook installs a native blocking detour at the dialogue function: it pauses the
game thread, looks the line up (community first, then optional MT with control-tag preservation
and box-width wrapping), writes the English back, and continues. Restores original bytes on exit.

Auto-detection reads the running game's environment, so the easiest setup is to **launch DQX
once** — `install_root` is then remembered (and refreshed if you reinstall elsewhere). No fixed
install location is required.

## Patch manifests

File patching is data-driven and **idempotent** (re-running is a no-op). A JSON manifest groups
`{target, url}` entries (`target` relative to the install root) into toggleable groups —
`game_files` (default), `config_exe`, `launcher_exe` — mirroring upstream's three patch
operations. Assets are pulled from GitHub "latest-release" redirects, so the manifest tracks
upstream automatically without hardcoding a version. Originals are backed up before each patch;
files the patch *adds* (e.g. `data00000000.win32.dat1`) are removed on `restore`. Point
`config.patch.manifest_url` at a hosted manifest to override the bundled
`src/dqxclarity/patching/data/default_manifest.json`.

## License & credits

dqxclarity-linux is licensed under the **GNU General Public License v2.0** — see [LICENSE](LICENSE).
This covers the dqxclarity-linux **source code** only.

It is an independent, Linux-native reimplementation inspired by
[**dqxclarity**](https://github.com/dqx-translation-project/dqxclarity) by the
dqx-translation-project, and at `sync` it consumes that community's translation **data** (the
`dqx_translations` / custom-translations corpora and proper-noun glossary). That data is the
property of its respective authors and is distributed under their own terms — it is **not** covered
by this repository's license. Huge thanks to the dqx-translation-project and the wider DQX
translation community; please support the upstream project.
