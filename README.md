# dqxclarity-linux

A Linux-native, CLI-only fork of [dqxclarity](https://github.com/dqx-translation-project/dqxclarity)
that translates **Dragon Quest X Online** running under Steam + Proton/Wine.

See [`PLAN.md`](PLAN.md) for the full design, decisions, and Phase 0 spike results.

## Status

- **Phase 0 (spike): done** — native memory read/write of the live (32-bit, WOW64) game proven
  via `process_vm_readv`/`writev` with no elevation; `rwxp` code caves present (native detour
  hooking viable).
- **Phase 1 (this MVP): static file patching** — install detection, manifest-driven download +
  verify + atomic replace with timestamped backups and a game-running safety block.
- **Phase 2+**: AOB scanner + signatures, then live translation (DB-cached + MT providers).

## Install

```sh
python -m venv .venv && . .venv/bin/activate   # fish: source .venv/bin/activate.fish
pip install -e .
# live-translation backend (later phases):
pip install -e '.[frida]'
```

## Usage

```sh
dqxclarity doctor                 # preflight: install detection, paths, permissions, deps
dqxclarity probe                  # inspect the running game + live memory-read smoke test
dqxclarity patch --dry-run        # show what file patching would change
dqxclarity patch                  # apply translated UI/menu archive (game must be CLOSED)
dqxclarity patch --config --launcher   # also patch DQXConfig.exe + DQXLauncher.exe
dqxclarity restore                # roll back to the most recent backup
dqxclarity scan                   # find/read Japanese text in the live game (diagnostic)
dqxclarity romanize "たろう"       # local JP->romaji demo (no game needed)
dqxclarity names                  # live-translate player/NPC names in the running game
dqxclarity sync                   # download the curated community dialogue dataset (~4k+ lines)
dqxclarity run                    # live-translate all surfaces (dialogue, quests, …) at once
dqxclarity run --hooks dialogue   # or just specific surfaces
dqxclarity translate-dialogue     # dialogue only (verbose community-vs-MT logging)
dqxclarity translate-text "…"     # translate one string via the configured provider
dqxclarity config show
dqxclarity config set install_root "/path/to/DRAGON QUEST X"
dqxclarity config set patch.manifest_url https://example/manifest.json
dqxclarity config set translate.provider claude_cli   # optional MT for uncovered text
```

## Live translation (Phase 3a)

Live translation intercepts Japanese the game shows, looks it up, and writes English back. The
sources, in order: (1) the **curated community DB** (human translations — the bulk), (2) local
**romaji** for player/NPC names via `pykakasi` (in-process, no service), and (3) an **optional MT
provider** only for the leftover untranslated tail. Each unique string is translated once and
cached forever (in-memory dict + SQLite-WAL).

**Two-tier MT** (both optional, off by default):
- `translate.provider` — a **fast, synchronous** translator for instant first-view (e.g.
  `googletranslatefree`: free Google, no key, ~200ms).
- `translate.upgrade_provider` — a **slow, higher-quality** translator (e.g. `claude_cli`, your
  Claude subscription) that runs in the **background and upgrades** the cache, so a re-viewed line
  shows the better translation. Quality only ever increases (community > claude > google); the
  curated human translations are never overwritten.

```sh
dqxclarity config set translate.provider googletranslatefree   # instant first-view
dqxclarity config set translate.upgrade_provider claude_cli     # background quality upgrade
```

### Dialogue (community DB + name placeholders)

Run `dqxclarity sync` once to pull the curated community dialogue dataset (`merge.xlsx`, ~4k+
human-translated lines) into the cache. Covered dialogue then renders **instantly and correctly
formatted** (it's human-made, with all of DQX's control codes intact). For name-bearing lines,
set your character/sibling names so the `<pnplacehold>`/`<snplacehold>` entries match for you:

```sh
dqxclarity config set translate.player_name_ja "タイカン"
dqxclarity config set translate.player_name_en "Taikan"
dqxclarity translate-dialogue        # community hit -> as-is; otherwise MT fallback
```

`translate-dialogue` installs a native blocking detour at the dialogue function: it pauses the
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
