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
| `mame_versions` | list | *(off)* | Restrict arcade ROMs to machines present in these versioned romsets (e.g. `[mame2003, mame2003-plus]`). Requires `arcade-import --version` for each listed version. Non-arcade systems are unaffected. |
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

#### MAME version romset filtering

On lower-end devices (RK3326 and similar) only specific libretro cores work — typically `mame2003_libretro` and `mame2003_plus_libretro`, each of which supports a fixed frozen romset. Import those XMLs under a version label to restrict exports to compatible ROMs only:

```bash
python3 romcurator.py arcade-import --xml mame2003.xml      --version mame2003
python3 romcurator.py arcade-import --xml mame2003-plus.xml --version mame2003-plus
```

Then filter at export time:

```bash
python3 romcurator.py build r36s --mame-versions mame2003,mame2003-plus --execute
```

Or bake the filter into the profile permanently:

```yaml
selection:
  mame_versions: [mame2003, mame2003-plus]
```

`--version` imports only store machine names (lightweight). Full metadata (for `arcade-analyze` stats and control-type filtering) still requires a separate unversioned `arcade-import` from a full MAME XML.

The `r36s`, `r39max`, and `odroidgosuper` profiles include `mame_versions` by default.

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

#### DAT file storage convention

Store MAME XML DAT files under `mame-xml/` using the libretro core name as the filename:

```text
mame-xml/
├── mame2000.xml        ← MAME 0.37b5
├── mame2003.xml        ← MAME 0.78
├── mame2003-plus.xml   ← MAME 0.78+ (mame2003-plus core)
├── mame2010.xml        ← MAME 0.139
├── mame2014.xml        ← MAME 0.159
├── mame2016.xml        ← MAME 0.174 (arcade only)
└── mame2016-home.xml   ← MAME 0.174 (home systems)
```

DAT files can be plain `.xml`/`.dat` or `.zip` archives containing one — both are accepted by `arcade-import` and `dat-check`.

#### dat-check — identify your romset version

Compare a ROM folder against one or more MAME XML DAT files to identify which version the romset is from. **Read-only — no database changes.**

```bash
python3 romcurator.py dat-check /mnt/storage/roms/arcade mame-xml/mame2016.xml
```

Check against multiple DATs at once for a side-by-side coverage comparison:

```bash
python3 romcurator.py dat-check /mnt/storage/roms/arcade \
    mame-xml/mame2003-plus.xml \
    mame-xml/mame2014.xml \
    mame-xml/mame2016.xml
```

Output columns:

| Column | Meaning |
|--------|---------|
| Machines (total) | All entries in the DAT including clones |
| Parents only | Unique games (no regional variants or clones) |
| Your folder has | Files in the folder recognised by this DAT |
| % of folder | Fraction of your files known to this DAT — **highest value = likely source version** |
| % of DAT parents | How complete your collection is relative to this DAT |

DATs with identical machine lists are automatically flagged (e.g. a mislabelled zip).

Add `--detail` to list files in the folder that are not found in any DAT:

```bash
python3 romcurator.py dat-check /mnt/storage/roms/arcade mame-xml/mame2016.xml --detail
```

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
python3 romcurator.py gen-m3u
python3 romcurator.py gen-m3u --systems psx,ps2,dreamcast
python3 romcurator.py gen-m3u --execute
python3 romcurator.py scan-systems
python3 romcurator.py compare-systems r36s
python3 romcurator.py profile-add r36s amiga500,amiga1200
python3 romcurator.py profile-remove r36s megadrive
python3 romcurator.py arcade-import --xml mame2003.xml --version mame2003
python3 romcurator.py arcade-import --xml mame2003-plus.xml --version mame2003-plus
python3 romcurator.py build r36s --mame-versions mame2003,mame2003-plus --execute
python3 romcurator.py dat-check /mnt/storage/roms/arcade mame-xml/mame2016.xml
python3 romcurator.py dat-check /mnt/storage/roms/arcade mame-xml/mame2003-plus.xml mame-xml/mame2016.xml
python3 romcurator.py dat-check /mnt/storage/roms/arcade mame-xml/mame2016.xml --detail
python3 romcurator.py folder-check /mnt/storage/roms/cps1 /mnt/storage/roms/arcade
python3 romcurator.py folder-check /mnt/storage/roms/cps1 /mnt/storage/roms/arcade --detail
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
│   ├── dat_check.py        ← compare ROM folder against MAME XML DAT files
│   ├── exporter.py         ← export plan, hardlink execution, arcade dedup
│   ├── folder_check.py     ← compare two ROM folders for duplicate detection
│   ├── inventory.py        ← scan orchestration
│   ├── mappings.py         ← systems.yaml loader and layout file loader
│   ├── parser.py           ← No-Intro/Redump filename parser
│   ├── profiles.py         ← profile loader and screen-fit display
│   ├── reporting.py        ← inventory and arcade reports
│   ├── romm_sync.py        ← ROMM API sync
│   ├── scanner.py          ← streaming filesystem walker
│   └── system_sync.py      ← system folder discovery and profile comparison
├── tools/
│   ├── zip_roms.py         ← compress uncompressed ROMs to zip
│   ├── dedup_roms.py       ← move duplicate-region ROMs to recycle bin
│   ├── clean_media.py      ← remove orphaned media/image/video files
│   └── gen_m3u.py          ← generate .m3u playlists for multi-disc games
├── mappings/
│   ├── systems.yaml        ← canonical system → NAS folder name + display metadata
│   └── layouts/            ← per-target folder aliases
│       ├── batocera.yaml
│       ├── emudeck.yaml
│       ├── r36s.yaml
│       └── romm.yaml
├── mame-xml/               ← MAME XML DAT files (named by libretro core)
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

#### gen-m3u

Generate `.m3u` playlist files for multi-disc games. The tool reads the
inventory database for ROMs that have a disc tag (`Disc 1`, `Side A`, `Tape 2`,
…) and writes one `.m3u` per game into the system's root folder.

```bash
python3 romcurator.py gen-m3u                          # dry-run all systems
python3 romcurator.py gen-m3u --systems psx,ps2        # dry-run specific systems
python3 romcurator.py gen-m3u --execute                # write .m3u files
```

Each `.m3u` lists disc filenames in disc-number order. Existing files are
compared against the expected content and shown as `CREATE`, `UPDATE`, or
`UNCHANGED` — only files that actually need changing are written.

Folder-based systems (switch, scummvm, etc.) are skipped; `.m3u` is relevant
for flat disc-image systems (PSX, PS2, Saturn, Dreamcast with CHDs, etc.).

Disc naming patterns recognised by the parser:

| Pattern | Examples |
|---------|---------|
| Standard No-Intro | `(Disc 1)`, `(Disk 2)`, `(Side A)`, `(Tape 1)`, `(Part 2)` |
| Region before disc | `(NA - Disc 1)`, `(EU - Disc 2)` |
| Disc before region | `(Disc 1 - EU)`, `(Disc 2 - English Patch)` |
| Amiga / C64 / MSX style | `Disk 1`, `Disk A`, `Disk1`, `DiskA`, `Disk 0` |

#### Recycle bin

All four archive maintenance tools move files to the recycle bin under their
original relative path:

```
<recycle_bin>/roms/<system>/<filename>
```

The recycle bin path is configured under `paths.recycle_bin` in `config.yaml` (default: `/mnt/storage/recycle_bin`).

### System Discovery

#### scan-systems

Scan the ROM root for subdirectories and compare against `mappings/systems.yaml`:

```bash
python3 romcurator.py scan-systems
```

Reports three categories:

| Category | Meaning |
|----------|---------|
| Known systems — folder present | In mappings and directory exists on disk |
| Known systems — folder absent | Defined in mappings but no directory found |
| Unknown folders | Directory exists but not in mappings — add to mappings or exclude |

Hidden directories (starting with `.`) are skipped automatically via
`scan.ignore_hidden`. Additional folders can be excluded permanently in
`config.yaml`:

```yaml
scan:
  exclude_system_folders: [.curator, .exports]
```

#### compare-systems

Compare discovered system folders against one profile's `include_systems` list:

```bash
python3 romcurator.py compare-systems r36s
```

Shows three categories with ready-to-run hint commands:

| Category | Action hint |
|----------|------------|
| Included (folder present) | Already in sync |
| Not in profile but folder present | `profile-add` hint printed |
| In profile but folder missing | `profile-remove` hint printed |

Use `profile-add` and `profile-remove` to act on the suggestions:

```bash
python3 romcurator.py profile-add r36s amiga500,amiga1200
python3 romcurator.py profile-remove r36s megadrive
```

#### folder-check — find duplicates across folders

Compare a source folder against a target folder to identify which files are already present before consolidating or deleting. **Read-only — no database changes, no files moved.**

```bash
python3 romcurator.py folder-check /mnt/storage/roms/cps1 /mnt/storage/roms/arcade
```

Each file in the source is categorised:

| Category | Meaning |
|----------|---------|
| ✓ Same name + size | Identical file already in target — safe to delete from source |
| ⚠ Same name, different size | Different ROM version (different CRC) — keep both, do not overwrite |
| ✗ Not in target | Only in source — would be lost if source folder is deleted |

Size mismatches are always printed. Add `--detail` to also list safe-to-delete and missing files:

```bash
python3 romcurator.py folder-check /mnt/storage/roms/cps1 /mnt/storage/roms/arcade --detail
```

Filter by extension (default: all common ROM extensions):

```bash
python3 romcurator.py folder-check /mnt/storage/roms/cps1 /mnt/storage/roms/arcade --ext zip,7z
```

Typical use: before consolidating separate sub-system folders (`cps1/`, `cps2/`, `neogeo/`) into a single `arcade/` folder, run `folder-check` on each to confirm all files are already present and flag any version mismatches.

## Not Implemented Yet

- `arcade_exclude_controls` has no effect until `arcade-import` is run from a full `mame -listxml` source — run `mame -listxml > mame_full.xml && python3 romcurator.py arcade-import --xml mame_full.xml --reset` to activate it
