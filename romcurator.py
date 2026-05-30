#!/usr/bin/env python3
"""ROM Curator CLI."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys

from core.compat import load_compat_lists
from core.compat_import import run_compat_import
from core.exporter import create_export_plan, execute_export_plan, print_export_plan, print_export_result
from core.inventory import run_inventory
from core.mappings import (
    load_layouts,
    load_system_mappings,
    print_system_mappings,
    validate_layouts,
    validate_system_mappings,
)
from core.profiles import (
    load_profiles,
    modify_profile_systems,
    print_profile_detail,
    print_profiles,
    validate_profile,
    validate_profiles,
)
from core.reporting import run_arcade_analyze, run_report


DEFAULT_CONFIG = {
    "paths": {
        "roms": "/mnt/storage/roms",
        "database": "/mnt/storage/curator/inventory.sqlite",
        "exports": "/mnt/storage/exports",
        "reports": "/mnt/storage/curator/reports",
        "recycle_bin": "/mnt/storage/recycle_bin",
        "mappings": str(Path(__file__).with_name("mappings") / "systems.yaml"),
        "profiles": str(Path(__file__).with_name("profiles")),
    },
    "scan": {
        "incremental": True,
        "ignore_hidden": True,
        "follow_symlinks": False,
        # Subfolder names inside roms root to ignore during system discovery.
        # Hidden folders (starting with .) are already skipped by ignore_hidden.
        "exclude_system_folders": [".curator", ".exports"],
    },
    "parsing": {
        "detect_regions": True,
        "detect_revision": True,
        "detect_tags": True,
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        apply_overrides(config, args)

        if args.command == "inventory":
            run_inventory(config, systems=_parse_systems(getattr(args, "systems", None)), mappings=_load_configured_mappings(config))
        elif args.command == "report":
            run_report(config, mappings=_load_configured_mappings(config), systems=_parse_systems(getattr(args, "systems", None)))
        elif args.command == "arcade-analyze":
            run_arcade_analyze(config, system=getattr(args, "system", None) or "arcade")
        elif args.command == "mappings":
            issues = run_mappings(config)
            if any(issue.level == "error" for issue in issues):
                return 1
        elif args.command == "profiles":
            issues = run_profiles(config)
            if _has_errors(issues):
                return 1
        elif args.command == "profile":
            issues = run_profile(config, args.name)
            if any(issue.level == "error" for issue in issues):
                return 1
        elif args.command == "explain":
            run_explain(config, args.name, systems=_parse_systems(args.systems), year_from=args.year_from, year_to=args.year_to, mame_versions=_parse_systems(getattr(args, "mame_versions", None)))
        elif args.command == "build":
            run_build(config, args.name, systems=_parse_systems(args.systems), year_from=args.year_from, year_to=args.year_to, execute=args.execute, rebuild=args.rebuild, yes=args.yes, mame_versions=_parse_systems(getattr(args, "mame_versions", None)), with_metadata=args.with_metadata)
        elif args.command == "sync":
            run_sync(config, args.name, systems=_parse_systems(args.systems), year_from=args.year_from, year_to=args.year_to, execute=args.execute, prune=args.prune, yes=args.yes, mame_versions=_parse_systems(getattr(args, "mame_versions", None)), with_metadata=args.with_metadata)
        elif args.command == "profile-add":
            run_profile_modify(config, args.name, add=_parse_systems(args.systems) or [], remove=[])
        elif args.command == "profile-remove":
            run_profile_modify(config, args.name, add=[], remove=_parse_systems(args.systems) or [])
        elif args.command == "romm-sync":
            from core.romm_sync import run_romm_sync
            mappings = _load_configured_mappings(config)
            layouts = _load_configured_layouts(config)
            run_romm_sync(config, mappings, reset=args.reset, layouts=layouts)
        elif args.command == "arcade-import":
            from core.arcade import run_arcade_import
            run_arcade_import(config, xml_path=getattr(args, "xml", None), reset=args.reset, version=getattr(args, "version", None))
        elif args.command == "zip-roms":
            from tools.zip_roms import run_zip_roms
            run_zip_roms(config, system=args.system, execute=args.execute)
        elif args.command == "dedup-roms":
            from tools.dedup_roms import run_dedup_roms
            regions = args.preferred_regions or None
            mappings = _load_configured_mappings(config)
            _skip_igdb = frozenset(
                s.strip() for part in (args.romm_skip_igdb or []) for s in part.split(",") if s.strip()
            )
            run_dedup_roms(
                config, mappings=mappings, system=args.system, preferred_regions=regions,
                execute=args.execute, romm_dupes=args.romm_dupes,
                romm_max_group=args.romm_max_group,
                romm_skip_igdb=_skip_igdb,
            )
        elif args.command == "rename-media":
            from tools.rename_media import run_rename_media
            media_folders = _parse_systems(args.media_folders)
            run_rename_media(
                config,
                systems=_parse_systems(args.systems),
                media_folders=media_folders,
                execute=args.execute,
                mappings=_load_configured_mappings(config),
            )
        elif args.command == "normalize-media":
            from tools.normalize_media import run_normalize_media
            src_folders = _parse_systems(args.source_folders)
            run_normalize_media(
                config,
                systems=_parse_systems(args.systems),
                source_folders=src_folders or None,
                rename_inline=args.rename_inline,
                clean_superseded=args.clean_superseded,
                execute=args.execute,
                mappings=_load_configured_mappings(config),
            )
        elif args.command == "clean-media":
            from tools.clean_media import run_clean_media
            media_folders = _parse_systems(args.media_folders)  # reuse comma-split helper
            run_clean_media(
                config,
                systems=_parse_systems(args.systems),
                media_folders=media_folders,
                remove_superseded=args.superseded,
                prefer_png=args.prefer_png,
                execute=args.execute,
                mappings=_load_configured_mappings(config),
            )
        elif args.command == "gen-gamelist":
            from tools.gen_gamelist import run_gen_gamelist
            mappings = _load_configured_mappings(config)
            systems_arg = _parse_systems(args.systems)
            if not systems_arg:
                roms_root = Path(str(config.get("paths", {}).get("roms", ""))).expanduser()
                systems_arg = sorted(d.name for d in roms_root.iterdir() if d.is_dir()) if roms_root.exists() else []
            run_gen_gamelist(config, systems_arg, mappings, dry_run=not args.execute)
        elif args.command == "gen-m3u":
            from tools.gen_m3u import run_gen_m3u
            mappings = _load_configured_mappings(config)
            run_gen_m3u(config, mappings=mappings, systems=_parse_systems(getattr(args, "systems", None)), execute=args.execute)
        elif args.command == "rom-rsync":
            from tools.rom_rsync import run_rom_rsync
            profiles = _load_configured_profiles(config)
            profile_data = profiles.get(args.profile, {})
            dest = args.dest or str((profile_data.get("rsync") or {}).get("dest") or "")
            if not dest:
                parser.error(
                    f"No destination specified. Pass --dest or set rsync.dest in profiles/{args.profile}.yaml"
                )
            run_rom_rsync(
                config,
                args.profile,
                dest,
                systems=_parse_systems(args.systems),
                delete=args.delete,
                execute=args.execute,
            )
        elif args.command == "nas-curate":
            from tools.nas_curate import run_nas_curate
            profiles = _load_configured_profiles(config)
            profile_data = profiles.get(args.profile, {})
            source = args.source or str((profile_data.get("rsync") or {}).get("dest") or "")
            if not source:
                parser.error(
                    f"No source specified. Pass --source or set rsync.dest in profiles/{args.profile}.yaml"
                )
            run_nas_curate(
                config,
                args.profile,
                source,
                systems=_parse_systems(args.systems),
                execute=args.execute,
            )
        elif args.command == "scan-systems":
            from core.system_sync import run_scan_systems
            mappings = _load_configured_mappings(config)
            run_scan_systems(config, mappings=mappings)
        elif args.command == "compare-systems":
            from core.system_sync import run_compare_systems
            mappings = _load_configured_mappings(config)
            profiles = _load_configured_profiles(config)
            run_compare_systems(config, args.name, mappings=mappings, profiles=profiles)
        elif args.command == "folder-check":
            from core.folder_check import run_folder_check, ROM_EXTENSIONS
            exts = frozenset(args.ext.lower().split(",")) if args.ext else ROM_EXTENSIONS
            run_folder_check(Path(args.source), Path(args.target), extensions=exts, detail=args.detail)
        elif args.command == "dat-check":
            from core.dat_check import run_dat_check
            dat_paths = [Path(d) for d in args.dats]
            run_dat_check(Path(args.folder), dat_paths, detail=args.detail, parents_only=args.parents_only)
        elif args.command == "fetch-media":
            from tools.fetch_media import run_fetch_media
            mappings = _load_configured_mappings(config)
            systems_arg = _parse_systems(args.systems)
            if not systems_arg:
                roms_root = Path(str(config.get("paths", {}).get("roms", ""))).expanduser()
                systems_arg = sorted(d.name for d in roms_root.iterdir() if d.is_dir()) if roms_root.exists() else []
            run_fetch_media(config, systems_arg, mappings, execute=args.execute)
        elif args.command == "compat-import":
            mappings_dir = _load_configured_mappings_dir(config)
            system_overrides = {}
            if args.system:
                if len(args.files) != 1:
                    print("Error: --system can only be used when importing a single file", file=sys.stderr)
                    return 1
                system_overrides[Path(args.files[0]).stem] = args.system
            run_compat_import(
                [Path(f) for f in args.files],
                chip=args.chip,
                mappings_dir=mappings_dir,
                system_overrides=system_overrides,
            )
        else:
            parser.error(f"Unknown command: {args.command}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ROM curation inventory tool")
    parser.add_argument(
        "--config",
        default=Path(__file__).with_name("config.yaml"),
        type=Path,
        help="Path to config.yaml",
    )
    parser.add_argument("--roms", help="Override ROM root path")
    parser.add_argument("--database", help="Override SQLite database path")
    parser.add_argument("--exports", help="Override exports root directory")
    parser.add_argument("--reports", help="Override reports directory")
    parser.add_argument("--recycle-bin", dest="recycle_bin", help="Override recycle bin path")
    parser.add_argument("--mappings", help="Override system mappings YAML path")
    parser.add_argument("--profiles", help="Override profiles directory")

    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory", help="Scan ROM archive into SQLite inventory")
    inventory_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only scan these system folders, comma-separated  (default: full archive)")
    report_parser = subparsers.add_parser("report", help="Print inventory report and save timestamped file")
    report_parser.add_argument("--reports", metavar="DIR", help="Directory to save timestamped report file")
    report_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only report on these systems, comma-separated  e.g. switch,ps3")
    arcade_analyze_parser = subparsers.add_parser("arcade-analyze", help="Summarize arcade inventory records")
    arcade_analyze_parser.add_argument("--reports", metavar="DIR", help="Directory to save timestamped report file")
    arcade_analyze_parser.add_argument("--system", metavar="SYSTEM", default="arcade", help="Inventory system to analyse (default: arcade)")
    subparsers.add_parser("mappings", help="Print and validate system mapping matrix")
    subparsers.add_parser("profiles", help="Print and validate device profiles")
    profile_parser = subparsers.add_parser("profile", help="Print one device profile in detail")
    profile_parser.add_argument("name", help="Profile name, for example r36s")
    profile_add_parser = subparsers.add_parser("profile-add", help="Add systems to a profile")
    profile_add_parser.add_argument("name", help="Profile name, for example r36s")
    profile_add_parser.add_argument("systems", metavar="SYSTEM,...", help="Comma-separated systems to add")
    profile_remove_parser = subparsers.add_parser("profile-remove", help="Remove systems from a profile")
    profile_remove_parser.add_argument("name", help="Profile name, for example r36s")
    profile_remove_parser.add_argument("systems", metavar="SYSTEM,...", help="Comma-separated systems to remove")
    explain_parser = subparsers.add_parser("explain", help="Explain selected ROMs for a profile")
    explain_parser.add_argument("name", help="Profile name, for example r36s")
    explain_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only include these systems, comma-separated  e.g. gba,snes,nes")
    explain_parser.add_argument("--from", dest="year_from", type=int, metavar="YEAR", help="Only include games released in this year or later")
    explain_parser.add_argument("--to", dest="year_to", type=int, metavar="YEAR", help="Only include games released in this year or earlier")
    explain_parser.add_argument("--mame-versions", metavar="VERSION,...", dest="mame_versions", help="Restrict arcade ROMs to these MAME version romsets, comma-separated  e.g. mame2003,mame2003-plus")
    build_parser = subparsers.add_parser("build", help="Build hardlink export for a profile")
    build_parser.add_argument("name", help="Profile name, for example r36s")
    build_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only export these systems, comma-separated  e.g. gba,snes,nes")
    build_parser.add_argument("--from", dest="year_from", type=int, metavar="YEAR", help="Only include games released in this year or later")
    build_parser.add_argument("--to", dest="year_to", type=int, metavar="YEAR", help="Only include games released in this year or earlier")
    build_parser.add_argument("--execute", action="store_true", help="Create hardlinks instead of dry-running")
    build_parser.add_argument("--rebuild", action="store_true", help="Delete this profile export before building")
    build_parser.add_argument("--yes", action="store_true", help="Confirm destructive rebuild behavior")
    build_parser.add_argument("--mame-versions", metavar="VERSION,...", dest="mame_versions", help="Restrict arcade ROMs to these MAME version romsets, comma-separated  e.g. mame2003,mame2003-plus")
    build_parser.add_argument("--with-metadata", action="store_true", dest="with_metadata", help="Include matching images, videos, and gamelist.xml in the manifest alongside ROMs")
    sync_parser = subparsers.add_parser("sync", help="Build export and optionally prune stale exported files")
    sync_parser.add_argument("name", help="Profile name, for example r36s")
    sync_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only sync these systems, comma-separated  e.g. gba,snes,nes")
    sync_parser.add_argument("--from", dest="year_from", type=int, metavar="YEAR", help="Only include games released in this year or later")
    sync_parser.add_argument("--to", dest="year_to", type=int, metavar="YEAR", help="Only include games released in this year or earlier")
    sync_parser.add_argument("--execute", action="store_true", help="Create hardlinks instead of dry-running")
    sync_parser.add_argument("--prune", action="store_true", help="Remove stale files from this profile export")
    sync_parser.add_argument("--yes", action="store_true", help="Confirm destructive prune behavior")
    sync_parser.add_argument("--mame-versions", metavar="VERSION,...", dest="mame_versions", help="Restrict arcade ROMs to these MAME version romsets, comma-separated  e.g. mame2003,mame2003-plus")
    sync_parser.add_argument("--with-metadata", action="store_true", dest="with_metadata", help="Include matching images, videos, and gamelist.xml in the manifest alongside ROMs")
    romm_sync_parser = subparsers.add_parser("romm-sync", help="Sync ROMM metadata into inventory.sqlite (name, rating, year, genres, summary, developer, publisher, cover/screenshot URLs)")
    romm_sync_parser.add_argument("--reset", action="store_true", help="Clear romm_roms table before syncing (full re-sync)")
    arcade_import_parser = subparsers.add_parser("arcade-import", help="Import MAME XML and classify arcade ROMs")
    arcade_import_parser.add_argument("--xml", metavar="PATH", help="Path to mame.xml (default: stream from mame binary)")
    arcade_import_parser.add_argument("--version", metavar="NAME", help="Store only machine names under this version label (e.g. mame2003, mame2003-plus) for export filtering.  Omit to import full metadata into mame_machines.")
    arcade_import_parser.add_argument("--reset", action="store_true", help="Clear the target table (or version slice) before importing")
    zip_roms_parser = subparsers.add_parser("zip-roms", help="Zip uncompressed ROMs, moving originals to recycle bin")
    zip_roms_parser.add_argument("--system", metavar="SYSTEM", help="Only process this system folder  (default: all)")
    zip_roms_parser.add_argument("--execute", action="store_true", help="Actually zip files  (default: dry run)")
    dedup_parser = subparsers.add_parser("dedup-roms", help="Recycle duplicate ROMs, keeping the preferred region")
    dedup_parser.add_argument("--system", metavar="SYSTEM", help="Only deduplicate this system  (default: all)")
    dedup_parser.add_argument(
        "--preferred-regions", nargs="+", metavar="REGION",
        help="Region priority, highest first  (default: USA Europe Japan)",
    )
    dedup_parser.add_argument("--execute", action="store_true", help="Actually move files  (default: dry run)")
    dedup_parser.add_argument(
        "--romm-dupes", action="store_true",
        help="Also report cross-title duplicates identified via ROMM igdb_id "
             "(same game with different market names, e.g. '90 Minutes - European Prime Goal' "
             "and 'J.League Soccer Prime Goal 3').  Groups containing hacks/betas are skipped. "
             "Requires romm-sync to have been run.",
    )
    dedup_parser.add_argument(
        "--romm-max-group", type=int, default=4, metavar="N",
        help="Skip ROMM groups with more than N members — large groups almost always "
             "indicate a ROMM bulk-import error (default: 4).",
    )
    dedup_parser.add_argument(
        "--romm-skip-igdb", nargs="+", metavar="ID",
        help="Comma- or space-separated igdb_ids to always skip in --romm-dupes mode. "
             "Useful for sequels or franchise entries that ROMM incorrectly collapses "
             "under one igdb_id.  Also reads dedup.romm_skip_igdb_ids from config.yaml.",
    )
    gen_gamelist_parser = subparsers.add_parser("gen-gamelist", help="Generate or update gamelist.xml for EmulationStation-compatible frontends (Batocera, ES-DE, EmuDeck)")
    gen_gamelist_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only process these systems, comma-separated  (default: all)")
    gen_gamelist_parser.add_argument("--execute", action="store_true", help="Actually write gamelist.xml files  (default: dry run)")
    fetch_media_parser = subparsers.add_parser("fetch-media", help="Download missing boxart/screenshot images from ROMM to NAS system folders (requires romm-sync)")
    fetch_media_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only process these systems, comma-separated  (default: all)")
    fetch_media_parser.add_argument("--execute", action="store_true", help="Actually download files  (default: dry run)")
    rename_media_parser = subparsers.add_parser("rename-media", help="Rename media files that have old ROM-set tags (e.g. '(E) [!]') to match current ROM naming")
    rename_media_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only process these systems, comma-separated  (default: all)")
    rename_media_parser.add_argument("--media-folders", metavar="FOLDER,...", help="Media subfolder names to check, comma-separated  (default: images,videos,snap,boxart,wheel,...)")
    rename_media_parser.add_argument("--execute", action="store_true", help="Actually rename files  (default: dry run)")
    normalize_media_parser = subparsers.add_parser("normalize-media", help="Consolidate wheel/, boxart/, snap/ etc. into images/ and videos/ with Batocera suffix naming")
    normalize_media_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only process these systems, comma-separated  (default: all)")
    normalize_media_parser.add_argument("--source-folders", metavar="FOLDER,...", help="Source subfolders to convert, comma-separated  (default: wheel,boxart,snap,cartart,marquee,fanarts,...)")
    normalize_media_parser.add_argument("--rename-inline", action="store_true", help="Also rename plain-stem files already in images/ and videos/ to add the suffix (images/1942.png → images/1942-image.png)")
    normalize_media_parser.add_argument("--clean-superseded", action="store_true", help="Also move superseded source files (where dest already exists) to recycle bin")
    normalize_media_parser.add_argument("--execute", action="store_true", help="Actually move/rename files  (default: dry run)")
    clean_media_parser = subparsers.add_parser("clean-media", help="Remove orphaned media files (images, videos, boxart, etc.) from the ROM archive")
    clean_media_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only check these systems, comma-separated  (default: all)")
    clean_media_parser.add_argument("--media-folders", metavar="FOLDER,...", help="Media subfolder names to check, comma-separated  (default: images,videos,snap,boxart,wheel,...)")
    clean_media_parser.add_argument("--superseded", action="store_true", help="Also remove plain-stem files in images/ and videos/ that are shadowed by a suffix-style version (e.g. drakton.png when drakton-image.png exists)")
    clean_media_parser.add_argument("--prefer-png", dest="prefer_png", action="store_true", help="Remove JPG/JPEG files that have a PNG counterpart with the same stem (e.g. 1942-image.jpg when 1942-image.png exists)")
    clean_media_parser.add_argument("--execute", action="store_true", help="Actually move files to recycle bin  (default: dry run)")
    gen_m3u_parser = subparsers.add_parser("gen-m3u", help="Generate .m3u playlist files for multi-disc games")
    gen_m3u_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only process these systems, comma-separated  (default: all)")
    gen_m3u_parser.add_argument("--execute", action="store_true", help="Actually write .m3u files  (default: dry run)")
    rom_rsync_parser = subparsers.add_parser("rom-rsync", help="Rsync a profile's hardlink export to a device (local mount or SSH)")
    rom_rsync_parser.add_argument("profile", help="Profile name matching a built export under paths.exports (e.g. r36s)")
    rom_rsync_parser.add_argument("--dest", metavar="DEST", help="Destination: local path (e.g. /run/media/user/SDCARD/roms) or SSH target (e.g. root@192.168.1.100:/recalbox/share/roms)  (default: rsync.dest from profile yaml)")
    rom_rsync_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only sync these system folders, comma-separated  (default: all)")
    rom_rsync_parser.add_argument("--delete", action="store_true", help="Remove files on the device that are no longer in the export (mirrors export exactly)  (default: off)")
    rom_rsync_parser.add_argument("--execute", action="store_true", help="Actually transfer files  (default: dry run)")
    nas_curate_parser = subparsers.add_parser("nas-curate", help="Interactively move to recycle bin NAS ROMs that were deleted from a synced device after playtesting")
    nas_curate_parser.add_argument("profile", help="Profile name whose export was synced to the device (e.g. r36s)")
    nas_curate_parser.add_argument("--source", metavar="SOURCE", help="Where the device's ROMs currently live: local path or SSH target (e.g. root@192.168.1.100:/recalbox/share/roms)  (default: rsync.dest from profile yaml)")
    nas_curate_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only curate these systems, comma-separated  (default: all)")
    nas_curate_parser.add_argument("--execute", action="store_true", help="Enter the interactive Y/N prompt and move confirmed files to recycle bin  (default: dry run, listing only)")
    subparsers.add_parser("scan-systems", help="Scan ROM root for new/removed/unknown system folders vs mappings")
    compare_systems_parser = subparsers.add_parser("compare-systems", help="Compare discovered system folders against a profile's include list")
    compare_systems_parser.add_argument("name", help="Profile name, for example r36s")
    folder_check_parser = subparsers.add_parser("folder-check", help="Check which files in a source folder are already present in a target folder")
    folder_check_parser.add_argument("source", help="Source folder to check (e.g. /mnt/storage/roms/cps1)")
    folder_check_parser.add_argument("target", help="Target folder to compare against (e.g. /mnt/storage/roms/arcade)")
    folder_check_parser.add_argument("--detail", action="store_true", help="Print full file lists for all categories")
    folder_check_parser.add_argument("--ext", metavar="EXT,...", help="Only check files with these extensions, comma-separated (default: all ROM extensions)")
    dat_check_parser = subparsers.add_parser("dat-check", help="Compare a ROM folder against one or more MAME XML DAT files to identify which version the romset is from")
    dat_check_parser.add_argument("folder", help="ROM folder to check (e.g. /mnt/storage/roms/arcade)")
    dat_check_parser.add_argument("dats", nargs="+", metavar="DAT", help="One or more MAME XML DAT files (.xml, .dat, or .zip containing one)")
    dat_check_parser.add_argument("--detail", action="store_true", help="Print files in folder not found in any DAT")
    dat_check_parser.add_argument("--parents-only", dest="parents_only", action="store_true", help="Only match parent ROMs (ignore clones)")

    compat_import_parser = subparsers.add_parser("compat-import", help="Import R36S/RK3326 compatibility xlsx lists into compat YAML files")
    compat_import_parser.add_argument("files", nargs="+", metavar="XLSX", help="One or more xlsx compatibility list files to import")
    compat_import_parser.add_argument("--chip", default="rk3326", help="Chip identifier used to organise compat files (default: rk3326)")
    compat_import_parser.add_argument("--system", metavar="SYSTEM", help="Override system name (only valid when importing a single file)")

    return parser


def load_config(config_path: Path) -> dict[str, object]:
    config = deepcopy(DEFAULT_CONFIG)

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read config.yaml. Install curator/requirements.txt") from exc

    for path in (config_path, config_path.with_name("config.local.yaml")):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config root must be a mapping: {path}")
        _deep_update(config, loaded)

    config["_config_dir"] = str(config_path.parent)
    return config


def apply_overrides(config: dict[str, object], args: argparse.Namespace) -> None:
    paths = config.setdefault("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    if args.roms:
        paths["roms"] = args.roms
    if args.database:
        paths["database"] = args.database
    if args.exports:
        paths["exports"] = args.exports
    if args.reports:
        paths["reports"] = args.reports
    if args.recycle_bin:
        paths["recycle_bin"] = args.recycle_bin
    if args.mappings:
        paths["mappings"] = args.mappings
    if args.profiles:
        paths["profiles"] = args.profiles


def run_mappings(config: dict[str, object]):
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")
    mappings_path = paths.get("mappings") or Path(__file__).with_name("mappings") / "systems.yaml"
    mappings_path = _resolve_config_path(config, str(mappings_path))
    mappings = load_system_mappings(mappings_path)
    layouts = _load_configured_layouts(config)
    issues = validate_system_mappings(mappings) + validate_layouts(layouts, mappings)
    print_system_mappings(mappings, issues, layouts=layouts)
    return issues


def run_profiles(config: dict[str, object]):
    mappings = _load_configured_mappings(config)
    layouts = _load_configured_layouts(config)
    profiles = _load_configured_profiles(config)
    validation = validate_profiles(profiles, mappings, layouts)
    print_profiles(profiles, mappings, validation)
    return validation


def run_profile(config: dict[str, object], name: str):
    mappings = _load_configured_mappings(config)
    layouts = _load_configured_layouts(config)
    profiles = _load_configured_profiles(config)
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise KeyError(f"Unknown profile '{name}'. Available profiles: {available}")
    issues = validate_profile(profiles[name], mappings, layouts)
    print_profile_detail(name, profiles[name], mappings, issues, layouts)
    return issues


def run_profile_modify(config: dict[str, object], name: str, *, add: list[str], remove: list[str]) -> None:
    mappings = _load_configured_mappings(config)
    profiles = _load_configured_profiles(config)
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise KeyError(f"Unknown profile '{name}'. Available profiles: {available}")

    profile_path = profiles[name].get("_path")
    if not profile_path:
        raise RuntimeError(f"Cannot determine file path for profile '{name}'")

    result = modify_profile_systems(profile_path, add=add, remove=remove, mappings=mappings)

    if result["unknown"]:
        print(f"Warning: unknown systems (not in mappings matrix): {', '.join(result['unknown'])}")
    if result["added"]:
        print(f"Added to include_systems:    {', '.join(result['added'])}")
    if result["removed"]:
        print(f"Removed from profile:        {', '.join(result['removed'])}")
    if result["already_present"]:
        print(f"Already in profile:          {', '.join(result['already_present'])}")
    if result["not_found"]:
        print(f"Not found in profile:        {', '.join(result['not_found'])}")
    if not result["added"] and not result["removed"]:
        print("No changes made.")
    else:
        print(f"Profile saved: {profile_path}")


def run_explain(config: dict[str, object], name: str, *, systems: list[str] | None = None, year_from: int | None = None, year_to: int | None = None, mame_versions: list[str] | None = None):
    plan = _create_configured_export_plan(config, name, systems=systems, year_from=year_from, year_to=year_to, mame_versions=mame_versions)
    print_export_plan(plan)
    return plan


def run_build(config: dict[str, object], name: str, *, systems: list[str] | None = None, year_from: int | None = None, year_to: int | None = None, execute: bool, rebuild: bool, yes: bool, mame_versions: list[str] | None = None, with_metadata: bool = False):
    plan = _create_configured_export_plan(config, name, systems=systems, year_from=year_from, year_to=year_to, mame_versions=mame_versions)
    print_export_plan(plan)
    result = execute_export_plan(plan, dry_run=not execute, rebuild=rebuild, yes=yes, with_metadata=with_metadata)
    print_export_result(result)
    return result


def run_sync(config: dict[str, object], name: str, *, systems: list[str] | None = None, year_from: int | None = None, year_to: int | None = None, execute: bool, prune: bool, yes: bool, mame_versions: list[str] | None = None, with_metadata: bool = False):
    plan = _create_configured_export_plan(config, name, systems=systems, year_from=year_from, year_to=year_to, mame_versions=mame_versions)
    print_export_plan(plan)
    result = execute_export_plan(plan, dry_run=not execute, prune=prune, yes=yes, with_metadata=with_metadata)
    print_export_result(result)
    return result


def _create_configured_export_plan(config: dict[str, object], name: str, *, systems: list[str] | None = None, year_from: int | None = None, year_to: int | None = None, mame_versions: list[str] | None = None):
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    database_path = paths.get("database")
    exports_path = paths.get("exports")
    roms_path = paths.get("roms")
    if not database_path:
        raise ValueError("Config key paths.database is required")
    if not exports_path:
        raise ValueError("Config key paths.exports is required")

    mappings = _load_configured_mappings(config)
    profiles = _load_configured_profiles(config)
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise KeyError(f"Unknown profile '{name}'. Available profiles: {available}")

    layouts = _load_configured_layouts(config)
    issues = validate_profile(profiles[name], mappings, layouts)
    errors = [issue.message for issue in issues if issue.level == "error"]
    if errors:
        raise ValueError("; ".join(errors))

    # Apply CLI year overrides on top of profile selection without mutating the
    # cached profile dict (deepcopy already done in load_profiles).
    profile = profiles[name]
    if year_from is not None or year_to is not None:
        profile = {**profile, "selection": {**(profile.get("selection") or {}), **({} if year_from is None else {"year_from": year_from}), **({} if year_to is None else {"year_to": year_to})}}

    # Load compat lists for the chip named in the profile's selection.compat_chip.
    compat_chip = None
    selection = profile.get("selection")
    if isinstance(selection, dict):
        compat_chip = selection.get("compat_chip")
    compat_lists = {}
    if compat_chip:
        mappings_dir = _load_configured_mappings_dir(config)
        compat_lists = load_compat_lists(mappings_dir, str(compat_chip))

    return create_export_plan(
        _resolve_config_path(config, str(database_path)),
        name,
        profile,
        mappings,
        _resolve_config_path(config, str(exports_path)),
        roms_root=_resolve_config_path(config, str(roms_path)) if roms_path else None,
        systems_filter=systems,
        mame_versions=mame_versions,
        layouts=layouts,
        compat_lists=compat_lists,
    )


def _load_configured_mappings(config: dict[str, object]) -> dict[str, dict[str, object]]:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")
    mappings_path = paths.get("mappings") or Path(__file__).with_name("mappings") / "systems.yaml"
    return load_system_mappings(_resolve_config_path(config, str(mappings_path)))


def _load_configured_mappings_dir(config: dict[str, object]) -> Path:
    """Return the directory containing systems.yaml (and the compat/ sub-dir)."""
    paths = config.get("paths", {})
    mappings_path = paths.get("mappings") if isinstance(paths, dict) else None
    mappings_path = mappings_path or Path(__file__).with_name("mappings") / "systems.yaml"
    return _resolve_config_path(config, str(mappings_path)).parent


def _load_configured_layouts(config: dict[str, object]) -> dict[str, dict[str, list[str]]]:
    """Load layout files from the layouts/ directory alongside systems.yaml."""
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        return {}
    mappings_path = paths.get("mappings") or Path(__file__).with_name("mappings") / "systems.yaml"
    layouts_dir = _resolve_config_path(config, str(mappings_path)).parent / "layouts"
    return load_layouts(layouts_dir)


def _load_configured_profiles(config: dict[str, object]) -> dict[str, dict[str, object]]:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")
    profiles_path = paths.get("profiles") or Path(__file__).with_name("profiles")
    return load_profiles(_resolve_config_path(config, str(profiles_path)))


def _has_errors(validation) -> bool:
    return any(issue.level == "error" for issues in validation.values() for issue in issues)


def _resolve_config_path(config: dict[str, object], value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    config_dir = Path(str(config.get("_config_dir", Path(__file__).parent)))
    return config_dir / path


def _parse_systems(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _deep_update(base: dict[str, object], updates: dict[str, object]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)  # type: ignore[arg-type]
        else:
            base[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
