# ROM Curator

![ROM Curator](images/rom-curator.png)

ROM Curator is a Python tool for inventorying and eventually exporting curated
views of a large retro ROM archive stored on a NAS.

The project is being built in phases. The current implementation supports safe
metadata inventory, system mapping, device profiles, reporting, and cautious
hardlink export builds. It does not modify ROM files.

## Goals

- Keep one canonical master ROM archive.
- Inventory large ROM libraries safely, including multi-terabyte NAS archives.
- Support target ecosystems such as EmuDeck, R36S/R39 Max, Batocera, and ROMM.
- Normalize system names through a mapping matrix instead of hardcoded folder logic.
- Create curated exports using hardlinks only.

## Safety Rules

- Inventory scans do not modify ROM files.
- No files are moved, copied, renamed, or deleted from the ROM archive.
- SQLite metadata is the only thing updated during inventory.
- Export commands only write under the configured exports directory.
- `build` and `sync` dry-run unless `--execute` is passed.
- Destructive export cleanup requires explicit confirmation with `--yes`.

## Current Features

### Inventory

Run a streaming filesystem scan of the configured ROM archive:

```bash
python3 romcurator.py inventory
```

Scope the scan to one or more system folders to pick up changes quickly
without walking the entire archive:

```bash
python3 romcurator.py inventory --systems switch
python3 romcurator.py inventory --systems gba,nes,snes
```

When `--systems` is used, stale-row removal is also scoped to those folders
so the rest of the database is never touched.

The scanner captures:

- system from the top-level folder
- filename
- extension
- absolute path
- relative path
- size
- modified time
- parsed title
- region
- revision
- beta/prototype/translation/hack flags

The scanner is designed for large libraries:

- uses streaming `os.walk`
- does not build a giant file list in memory
- avoids hashing large ROM files
- uses `size:mtime` scan keys
- commits SQLite writes in batches

### Incremental Rescans

The inventory database tracks every file in a `scan_state` table:

```sql
scan_state(path, scan_key, last_seen)
```

On later scans, unchanged files are skipped while their `last_seen` timestamp is
updated. If a file disappears from disk, stale rows are removed from both
`roms` and `scan_state` after the scan completes.

### SQLite Scaling

The database layer enables performance-oriented SQLite settings:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=30000000000;
```

The database currently uses these tables:

- `roms`
- `scan_state`

Indexes exist for common query paths such as system, title, region, extension,
path, and scan state freshness.

### Filename Parsing

The parser handles common ROM naming tags such as:

```text
Chrono Trigger (USA) (Rev 1).sfc
```

It extracts useful metadata such as:

- title: `Chrono Trigger`
- region: `USA`
- revision: `Rev 1`
- beta/prototype/demo/translation/hack flags

### Reports

Generate inventory reports:

```bash
python3 romcurator.py report
```

Scope a report to one or more systems:

```bash
python3 romcurator.py report --systems switch
python3 romcurator.py report --systems switch,ps3
```

Reports include:

- total files / games
- total size
- systems by size (shows game count for folder-based systems, file count otherwise)
- extension breakdown
- largest ROMs
- region breakdown
- possible duplicates

Scoped reports have no row-count limit, drop the redundant Systems by Size
table when only one system is requested, and are saved with the system
name(s) in the filename.

### System Mapping Matrix

System aliases live in:

```text
mappings/systems.yaml
```

Print and validate the mapping matrix:

```bash
python3 romcurator.py mappings
```

The matrix maps canonical ROM Curator names to target ecosystem folders:

- NAS
- ROMM
- EmuDeck
- R36S/R39 Max
- Batocera

#### Folder-based systems

Some systems store each game as a subfolder containing multiple files
(e.g. ScummVM data files, DOS games, Switch titles with update packages).
Two flags control how these are handled:

| Flag | Systems | Effect |
|------|---------|--------|
| `folder_based: true` | scummvm, dos, windows, megacd, switch | Game count uses unique subfolders instead of raw file count; exporter exports all files in a subfolder as one unit |
| `subfolder_exclude: true` | scummvm, dos, windows, megacd | Subfolder files are untagged game data — excluded from region breakdown and duplicate detection |

Switch uses `folder_based` only (not `subfolder_exclude`) because its
depth-3 files are the actual ROM with proper No-Intro naming (including
region tags). Only depth-4+ files (`GameName/updates/`) are excluded.

### Device Profiles

Device profile rules live in:

```text
profiles/
```

Current profiles:

- `batocera.yaml`: bartop arcade cabinet — i5-8500T, 1 stick, 12 buttons, wireless keyboard+trackpad
- `steamdeck.yaml`: Steam Deck LCD — EmuDeck, dual analogue + trackpads
- `r36s.yaml`: R36S handheld — 640×480, dual analogue, 100 games/system cap
- `r39max.yaml`: R39 Max handheld — 720×720 square screen, dual analogue
- `odroidgosuper.yaml`: Odroid Go Super — 854×480, dual analogue

Print and validate all profiles:

```bash
python3 romcurator.py profiles
```

Inspect one profile and its target folder aliases:

```bash
python3 romcurator.py profile r36s
```

Profiles drive export planning and hardlink builds.

### Export Engine

Explain what a profile would export:

```bash
python3 romcurator.py explain r36s
```

Dry-run an export build:

```bash
python3 romcurator.py build r36s
```

Create hardlinks:

```bash
python3 romcurator.py build r36s --execute
```

Exports are written under:

```text
<exports>/<profile-name>/<target-system>/
```

For example:

```text
/mnt/storage/exports/r36s/gba/
/mnt/storage/exports/steamdeck/genesis/
```

The export engine:

- uses hardlinks only
- never copies ROM data
- selects one preferred region per title
- skips beta/prototype/hack files unless the profile allows them
- filters by ROMM metadata when `romm-sync` has been run (see below)
- honors `max_games_per_system`
- refuses to overwrite conflicting existing files
- supports `--rebuild --yes` for a profile export directory
- supports `sync --prune --yes` for stale exported files

Profile `selection:` keys that drive export filtering:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `include_beta` | bool | `false` | Include beta ROMs |
| `include_proto` | bool | `false` | Include prototype ROMs |
| `include_hacks` | bool | `false` | Include ROM hacks |
| `include_translations` | bool | `true` | Include fan translations |
| `arcade_dedupe` | bool | `true` | Group MAME clones by parent — export one ROM per unique game |
| `arcade_skip_non_playable` | bool | `true` | Skip BIOS chips, devices, and mechanical (AWP/fruit machine) ROMs |
| `arcade_exclude_controls` | list | `[]` | Skip arcade games needing listed MAME control types (e.g. `[wheel, spinner, trackball, lightgun]`). Has no effect until `arcade-import` is run from a full `mame -listxml` source. |
| `year_from` | int | *(off)* | Skip games released before this year. Games with no year data always pass. Can be overridden per-run with `--from YEAR`. |
| `year_to` | int | *(off)* | Skip games released after this year. Games with no year data always pass. Can be overridden per-run with `--to YEAR`. |
| `min_rating` | number | *(off)* | Skip ROMs with a real IGDB score below this value. Unrated ROMs (`total_rating = 0`) and ROMs with no ROMM record always pass. |
| `identified_only` | bool | `false` | Skip ROMs that ROMM considers unidentified. ROMs with no ROMM record always pass. |

ROMM-based filters require `romm-sync` to have been run first. If the `romm_roms`
table is empty, `min_rating` and `identified_only` have no effect.

### Multi-Disc Game Support

The inventory parser detects disc tags from filenames and stores them in a `disc` column:

```text
Final Fantasy VII (Europe) (Disc 1).cue  →  disc = "(Disc 1)"
Final Fantasy VII (Europe) (Disc 2).cue  →  disc = "(Disc 2)"
```

Supported patterns: `(Disc N)`, `(Disk N)`, `(Side A/B)`, `(Tape N)`, `(Part N)`.

During export, each disc is treated as an independent selection slot, so all discs
of a multi-disc game are included rather than only the first one being picked.

### ROMM Metadata Sync

Fetch and cache ROMM metadata into `inventory.sqlite`:

```bash
python3 romcurator.py romm-sync
python3 romcurator.py romm-sync --reset  # wipe and re-sync
```

ROMM metadata is stored in a `romm_roms` table and joined to the `roms` table
on `(canonical_system, filename)` at query time. ROMM is never queried during
inventory scans or export builds — only when `romm-sync` is explicitly run.

Requires `ROMM_URL` and `ROMM_TOKEN` in a `.env` file at the project root
(copy `.env.example` to `.env` and fill in both values).

Cached fields per ROM:

- `total_rating`, `aggregated_rating` — IGDB scores
- `is_identified` — whether ROMM matched this ROM to metadata
- `genres`, `themes`, `game_modes`, `player_count`
- `year`, `hltb_main`, `hltb_main_extra`, `hltb_completionist`
- `sibling_count`, `has_cover`, `regions`, `tags`

### Arcade Classification

Import MAME machine metadata and classify arcade ROMs by sub-system:

```bash
python3 romcurator.py arcade-import              # stream from installed mame binary
python3 romcurator.py arcade-import --xml /path/to/mame.xml   # use cached XML file
python3 romcurator.py arcade-import --reset      # wipe mame_machines before importing
```

Generates a pre-computed `mame.xml` with:

```bash
mame -listxml > /tmp/mame.xml
```

After import, arcade ROMs in the inventory are classified into sub-system buckets:

| Sub-system | Source |
|------------|--------|
| `cps1` | `capcom/cps1.cpp` |
| `cps2` | `capcom/cps2.cpp` |
| `cps3` | `capcom/cps3.cpp` |
| `neogeo` | `neogeo/neogeo.cpp` |
| `naomi` | `sega/naomi.cpp` |
| `naomi2` | `sega/naomi2.cpp` |
| `atomiswave` | `sega/atomiswave.cpp` |
| `mame` | everything else |

**Export routing**: when a profile lists `cps2` (or any other sub-system) as a system, classified arcade ROMs are automatically routed to that folder in the export — without touching or reorganizing the master archive.

Arcade ROMs that don't match a requested sub-system fall back to the `arcade` folder (if `arcade` is in the profile).

Classification is stored in the `arcade_system` column of the `roms` table and the `mame_machines` table. Re-run `arcade-import` to refresh after a MAME update.

## Commands

From the repository root:

```bash
python3 romcurator.py inventory
python3 romcurator.py inventory --systems switch
python3 romcurator.py inventory --systems gba,nes,snes
python3 romcurator.py report
python3 romcurator.py report --systems switch
python3 romcurator.py report --systems switch,ps3
python3 romcurator.py arcade-analyze
python3 romcurator.py arcade-import
python3 romcurator.py arcade-import --xml /path/to/mame.xml
python3 romcurator.py mappings
python3 romcurator.py profiles
python3 romcurator.py profile r36s
python3 romcurator.py explain r36s
python3 romcurator.py build r36s
python3 romcurator.py build r36s --execute
python3 romcurator.py sync r36s --execute --prune --yes
python3 romcurator.py romm-sync
python3 romcurator.py romm-sync --reset
python3 romcurator.py zip-roms
python3 romcurator.py zip-roms --system gba --execute
python3 romcurator.py dedup-roms
python3 romcurator.py dedup-roms --system snes --execute
python3 romcurator.py clean-media
python3 romcurator.py clean-media --systems snes --execute
python3 romcurator.py clean-media --systems snes,nes --media-folders boxart,wheel --execute
```

Useful overrides (e.g. for testing against a small sample tree):

```bash
python3 romcurator.py --roms /path/to/roms-test --database /tmp/test.sqlite inventory
python3 romcurator.py --database /tmp/test.sqlite report
python3 romcurator.py --database /tmp/test.sqlite --exports /tmp/exports explain r36s
```

## Configuration

Default config lives at:

```text
config.yaml
```

Important keys:

```yaml
paths:
  roms: /mnt/storage/roms
  database: /mnt/storage/curator/inventory.sqlite
  exports: /mnt/storage/exports
  reports: /mnt/storage/curator/reports
  recycle_bin: /mnt/storage/recycle_bin
  mappings: mappings/systems.yaml
  profiles: profiles

scan:
  incremental: true
  ignore_hidden: true
  follow_symlinks: false
```

For first tests, point `--roms` at a small sample tree instead of the full NAS
archive.

## Ignored Files

Inventory skips common non-ROM clutter:

- `.DS_Store`
- `Thumbs.db`
- `desktop.ini`
- `._*`
- `.srm`
- `.state`

It also ignores hidden directories and selected non-ROM directories such as:

- `.git`
- `cache`
- `savestates`

## Installation

Python 3.11+ is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies:

- PyYAML
- rich
- httpx (required for `romm-sync`)
- python-dotenv (required for `romm-sync`)

## Project Layout

```text
rom-curator/
├── romcurator.py           ← entry point
├── config.yaml             ← paths, scan settings, ROMM page_size
├── requirements.txt
├── .env.example            ← copy to .env, add ROMM_URL and ROMM_TOKEN
├── core/
│   ├── arcade.py           ← MAME XML parser, arcade sub-system classifier
│   ├── database.py         ← SQLite layer (roms, mame_machines, romm_roms)
│   ├── exporter.py         ← export plan, hardlink execution, arcade dedup
│   ├── inventory.py        ← scan orchestration
│   ├── mappings.py         ← systems.yaml loader
│   ├── parser.py           ← No-Intro/Redump filename parser
│   ├── profiles.py         ← profile loader and screen-fit display
│   ├── reporting.py        ← inventory and arcade reports
│   ├── romm_sync.py        ← ROMM API sync
│   └── scanner.py          ← streaming filesystem walker
├── tools/
│   ├── zip_roms.py         ← compress uncompressed ROMs to zip
│   ├── dedup_roms.py       ← move duplicate-region ROMs to recycle bin
│   └── clean_media.py      ← remove orphaned media/image/video files
├── mappings/
│   └── systems.yaml        ← canonical system → target folder matrix
├── profiles/
│   ├── batocera.yaml
│   ├── odroidgosuper.yaml
│   ├── r36s.yaml
│   ├── r39max.yaml
│   └── steamdeck.yaml
└── config/
    └── excluded_extensions.yaml  ← non-ROM extensions skipped by scanner
```

### Archive Maintenance Tools

These commands modify the NAS source archive. They are **dry-run only** by default and require `--execute` to make real changes. Files are always moved to the recycle bin rather than deleted.

#### zip-roms

Compress uncompressed single-file ROMs (.nes, .sfc, .gba, etc.) into individual .zip archives in place. The original is moved to the recycle bin after the zip is verified.

```bash
python3 romcurator.py zip-roms               # dry-run all systems
python3 romcurator.py zip-roms --system gba  # dry-run one system
python3 romcurator.py zip-roms --execute     # actually zip
```

CD-ROM formats (.bin/.cue, .iso, .img) are intentionally skipped — they involve companion files and need manual handling. After execution, re-run `inventory` to update the database.

#### dedup-roms

Identify duplicate ROMs (same title, multiple regions or variants) using inventory database metadata, and move the lower-priority copies to the recycle bin.

```bash
python3 romcurator.py dedup-roms                             # dry-run all systems
python3 romcurator.py dedup-roms --system snes               # dry-run one system
python3 romcurator.py dedup-roms --preferred-regions USA Europe Japan --execute
```

Priority ordering (highest wins):
1. Region — matches `--preferred-regions` order
2. Not-beta > beta
3. Not-proto > proto
4. Not-hack > hack
5. Compressed format (.zip > .7z > .chd > .cso > .pbp > .iso > .bin > .img > raw)
6. Filename alphabetical

Files that are never considered duplicates:
- `.cue`, `.gdi`, `.sub`, `.sbi`, `.m3u` — companion/cuesheet files that must travel with their primary disc image
- Files inside game subfolders of `folder_based` systems (e.g. ScummVM data files, megacd audio tracks, switch update packages)

Run `inventory` to rebuild the database after execution.

#### clean-media

Remove orphaned media files — images, videos, boxart, wheel art, and other
scraper assets — whose corresponding ROM no longer exists in the inventory.

```bash
python3 romcurator.py clean-media                            # dry-run all systems
python3 romcurator.py clean-media --systems snes             # dry-run one system
python3 romcurator.py clean-media --systems snes,nes --execute
python3 romcurator.py clean-media --media-folders boxart,wheel --execute
```

Scanned subfolders (default, all configurable via `--media-folders`):
`images`, `videos`, `snap`, `boxart`, `wheel`, `cartart`, `mixart`,
`manuals`, `logos`, `fanarts`, `backcovers`, `screenshots`, `marquees`, `media`

Two naming conventions are matched automatically:

| Convention | Example | Matched against |
|---|---|---|
| Full ROM stem | `7th Saga, The (USA).png` | ROM filename stem |
| Scraper suffix | `7th Saga, The-image.png` | Parsed ROM title after stripping `-image`/`-thumb`/`-marquee`/`-video`/… |

System files (`Thumbs.db`, `.DS_Store`, `gamelist.xml`, etc.) are always skipped.

Run `inventory --systems <system>` first to ensure the database is up to date
before executing, so recently added ROMs are not incorrectly flagged.

#### Recycle bin

All three archive maintenance tools move files to the recycle bin under their
original relative path:

```
<recycle_bin>/roms/<system>/<filename>
```

The recycle bin path is configured under `paths.recycle_bin` in `config.yaml` (default: `/mnt/storage/recycle_bin`).

## Not Implemented Yet

- `arcade_exclude_controls` has no effect until `arcade-import` is run from a full `mame -listxml` source — run `mame -listxml > mame_full.xml && python3 romcurator.py arcade-import --xml mame_full.xml --reset` to activate it
