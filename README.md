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
python3 curator/romcurator.py inventory
```

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
python3 curator/romcurator.py report
```

Reports include:

- total files
- total size
- systems by size
- extension breakdown
- largest ROMs
- region breakdown
- possible duplicates

### System Mapping Matrix

System aliases live in:

```text
curator/mappings/systems.yaml
```

Print and validate the mapping matrix:

```bash
python3 curator/romcurator.py mappings
```

The matrix maps canonical ROM Curator names to target ecosystem folders:

- NAS
- ROMM
- EmuDeck
- R36S/R39 Max
- Batocera

The R36S/R39 Max mapping has been expanded from `r36s_roms_structure.md`, with
housekeeping folders intentionally excluded.

### Device Profiles

Device profile rules live in:

```text
curator/profiles/
```

Current profiles:

- `steamdeck.yaml`: high-performance EmuDeck profile
- `r36s.yaml`: curated R36S/R39 Max profile with `max_games_per_system: 100`
- `batocera.yaml`: broad Batocera profile

Print and validate all profiles:

```bash
python3 curator/romcurator.py profiles
```

Inspect one profile and its target folder aliases:

```bash
python3 curator/romcurator.py profile r36s
```

Profiles drive export planning and hardlink builds.

### Export Engine

Explain what a profile would export:

```bash
python3 curator/romcurator.py explain r36s
```

Dry-run an export build:

```bash
python3 curator/romcurator.py build r36s
```

Create hardlinks:

```bash
python3 curator/romcurator.py build r36s --execute
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
python3 curator/romcurator.py romm-sync
python3 curator/romcurator.py romm-sync --reset  # wipe and re-sync
```

ROMM metadata is stored in a `romm_roms` table and joined to the `roms` table
on `(canonical_system, filename)` at query time. ROMM is never queried during
inventory scans or export builds — only when `romm-sync` is explicitly run.

Requires `ROMM_TOKEN` in a `.env` file at `curator/.env` or the project root.
The ROMM URL is configured in `config.yaml` under `romm.url`.

Cached fields per ROM:

- `total_rating`, `aggregated_rating` — IGDB scores
- `is_identified` — whether ROMM matched this ROM to metadata
- `genres`, `themes`, `game_modes`, `player_count`
- `year`, `hltb_main`, `hltb_main_extra`, `hltb_completionist`
- `sibling_count`, `has_cover`, `regions`, `tags`

### Arcade Classification

Import MAME machine metadata and classify arcade ROMs by sub-system:

```bash
python3 curator/romcurator.py arcade-import              # stream from installed mame binary
python3 curator/romcurator.py arcade-import --xml /path/to/mame.xml   # use cached XML file
python3 curator/romcurator.py arcade-import --reset      # wipe mame_machines before importing
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
python3 curator/romcurator.py romm-sync
python3 curator/romcurator.py romm-sync --reset
python3 curator/romcurator.py inventory
python3 curator/romcurator.py report
python3 curator/romcurator.py arcade-analyze
python3 curator/romcurator.py arcade-import
python3 curator/romcurator.py arcade-import --xml /path/to/mame.xml
python3 curator/romcurator.py mappings
python3 curator/romcurator.py profiles
python3 curator/romcurator.py profile r36s
python3 curator/romcurator.py explain r36s
python3 curator/romcurator.py build r36s
python3 curator/romcurator.py build r36s --execute
python3 curator/romcurator.py sync r36s --execute --prune --yes
python3 curator/romcurator.py zip-roms
python3 curator/romcurator.py zip-roms --system gba --execute
python3 curator/romcurator.py dedup-roms
python3 curator/romcurator.py dedup-roms --system snes --execute
```

Useful overrides:

```bash
python3 curator/romcurator.py --roms roms-test --database curator/inventory.sqlite inventory
python3 curator/romcurator.py --database curator/inventory.sqlite report
python3 curator/romcurator.py --mappings curator/mappings/systems.yaml mappings
python3 curator/romcurator.py --profiles curator/profiles profiles
python3 curator/romcurator.py --database curator/inventory.sqlite --exports exports explain r36s
```

## Configuration

Default config lives at:

```text
curator/config.yaml
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
pip install -r curator/requirements.txt
```

Dependencies:

- PyYAML
- rich
- httpx (required for `romm-sync`)
- python-dotenv (required for `romm-sync`)

## Project Layout

```text
curator/
├── romcurator.py
├── config.yaml
├── requirements.txt
├── core/
│   ├── arcade.py
│   ├── database.py
│   ├── exporter.py
│   ├── inventory.py
│   ├── mappings.py
│   ├── parser.py
│   ├── profiles.py
│   ├── reporting.py
│   ├── romm_sync.py
│   └── scanner.py
├── tools/
│   ├── zip_roms.py     ← zips uncompressed ROMs, moves originals to recycle bin
│   └── dedup_roms.py   ← moves duplicate-region ROMs to recycle bin
├── mappings/
│   └── systems.yaml
├── profiles/
│   ├── batocera.yaml
│   ├── r36s.yaml
│   └── steamdeck.yaml
├── reports/
└── cache/
```

### Archive Maintenance Tools

These commands modify the NAS source archive. They are **dry-run only** by default and require `--execute` to make real changes. Files are always moved to the recycle bin rather than deleted.

#### zip-roms

Compress uncompressed single-file ROMs (.nes, .sfc, .gba, etc.) into individual .zip archives in place. The original is moved to the recycle bin after the zip is verified.

```bash
python3 curator/romcurator.py zip-roms               # dry-run all systems
python3 curator/romcurator.py zip-roms --system gba  # dry-run one system
python3 curator/romcurator.py zip-roms --execute     # actually zip
```

CD-ROM formats (.bin/.cue, .iso, .img) are intentionally skipped — they involve companion files and need manual handling. After execution, re-run `inventory` to update the database.

#### dedup-roms

Identify duplicate ROMs (same title, multiple regions or variants) using inventory database metadata, and move the lower-priority copies to the recycle bin.

```bash
python3 curator/romcurator.py dedup-roms                             # dry-run all systems
python3 curator/romcurator.py dedup-roms --system snes               # dry-run one system
python3 curator/romcurator.py dedup-roms --preferred-regions USA Europe Japan --execute
```

Priority ordering (highest wins):
1. Region — matches `--preferred-regions` order
2. Not-beta > beta
3. Not-proto > proto
4. Not-hack > hack
5. Compressed format (.zip > .7z > .chd > raw)
6. Filename alphabetical

Run `inventory` to rebuild the database after execution.

#### Recycle bin

Both tools move files to the recycle bin under their original relative path:

```
<recycle_bin>/roms/<system>/<filename>
```

The recycle bin path is configured under `paths.recycle_bin` in `config.yaml` (default: `/mnt/storage/recycle_bin`).

## Not Implemented Yet

These are planned but intentionally not active yet:

- `max_games_per_system` cap counter in export plan summary (silently drops games, no counter shown)

## Documentation Maintenance

Update this README whenever new user-visible functionality is added, changed,
or removed. In practice, each implementation step should update:

- commands
- config keys
- generated files or database tables
- safety behavior
- implemented vs planned feature lists
