#!/usr/bin/env python3
"""ROM Curator CLI."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys

from core.exporter import create_export_plan, execute_export_plan, print_export_plan, print_export_result
from core.inventory import run_inventory
from core.mappings import load_system_mappings, print_system_mappings, validate_system_mappings
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
            run_inventory(config, systems=_parse_systems(getattr(args, "systems", None)))
        elif args.command == "report":
            run_report(config, mappings=_load_configured_mappings(config), systems=_parse_systems(getattr(args, "systems", None)))
        elif args.command == "arcade-analyze":
            run_arcade_analyze(config)
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
            run_build(config, args.name, systems=_parse_systems(args.systems), year_from=args.year_from, year_to=args.year_to, execute=args.execute, rebuild=args.rebuild, yes=args.yes, mame_versions=_parse_systems(getattr(args, "mame_versions", None)))
        elif args.command == "sync":
            run_sync(config, args.name, systems=_parse_systems(args.systems), year_from=args.year_from, year_to=args.year_to, execute=args.execute, prune=args.prune, yes=args.yes, mame_versions=_parse_systems(getattr(args, "mame_versions", None)))
        elif args.command == "profile-add":
            run_profile_modify(config, args.name, add=_parse_systems(args.systems) or [], remove=[])
        elif args.command == "profile-remove":
            run_profile_modify(config, args.name, add=[], remove=_parse_systems(args.systems) or [])
        elif args.command == "romm-sync":
            from core.romm_sync import run_romm_sync
            mappings = _load_configured_mappings(config)
            run_romm_sync(config, mappings, reset=args.reset)
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
            run_dedup_roms(config, mappings=mappings, system=args.system, preferred_regions=regions, execute=args.execute)
        elif args.command == "clean-media":
            from tools.clean_media import run_clean_media
            media_folders = _parse_systems(args.media_folders)  # reuse comma-split helper
            run_clean_media(config, systems=_parse_systems(args.systems), media_folders=media_folders, execute=args.execute)
        elif args.command == "gen-m3u":
            from tools.gen_m3u import run_gen_m3u
            mappings = _load_configured_mappings(config)
            run_gen_m3u(config, mappings=mappings, systems=_parse_systems(getattr(args, "systems", None)), execute=args.execute)
        elif args.command == "scan-systems":
            from core.system_sync import run_scan_systems
            mappings = _load_configured_mappings(config)
            profiles = _load_configured_profiles(config)
            run_scan_systems(config, mappings=mappings, profiles=profiles, apply=args.apply)
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
    sync_parser = subparsers.add_parser("sync", help="Build export and optionally prune stale exported files")
    sync_parser.add_argument("name", help="Profile name, for example r36s")
    sync_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only sync these systems, comma-separated  e.g. gba,snes,nes")
    sync_parser.add_argument("--from", dest="year_from", type=int, metavar="YEAR", help="Only include games released in this year or later")
    sync_parser.add_argument("--to", dest="year_to", type=int, metavar="YEAR", help="Only include games released in this year or earlier")
    sync_parser.add_argument("--execute", action="store_true", help="Create hardlinks instead of dry-running")
    sync_parser.add_argument("--prune", action="store_true", help="Remove stale files from this profile export")
    sync_parser.add_argument("--yes", action="store_true", help="Confirm destructive prune behavior")
    sync_parser.add_argument("--mame-versions", metavar="VERSION,...", dest="mame_versions", help="Restrict arcade ROMs to these MAME version romsets, comma-separated  e.g. mame2003,mame2003-plus")
    romm_sync_parser = subparsers.add_parser("romm-sync", help="Sync ROMM metadata into inventory.sqlite")
    romm_sync_parser.add_argument("--reset", action="store_true", help="Clear romm_roms table before syncing")
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
    clean_media_parser = subparsers.add_parser("clean-media", help="Remove orphaned media files (images, videos, boxart, etc.) from the ROM archive")
    clean_media_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only check these systems, comma-separated  (default: all)")
    clean_media_parser.add_argument("--media-folders", metavar="FOLDER,...", help="Media subfolder names to check, comma-separated  (default: images,videos,snap,boxart,wheel,...)")
    clean_media_parser.add_argument("--execute", action="store_true", help="Actually move orphaned files to recycle bin  (default: dry run)")
    gen_m3u_parser = subparsers.add_parser("gen-m3u", help="Generate .m3u playlist files for multi-disc games")
    gen_m3u_parser.add_argument("--systems", metavar="SYSTEM,...", help="Only process these systems, comma-separated  (default: all)")
    gen_m3u_parser.add_argument("--execute", action="store_true", help="Actually write .m3u files  (default: dry run)")
    scan_systems_parser = subparsers.add_parser("scan-systems", help="Detect new/removed system folders and sync profiles accordingly")
    scan_systems_parser.add_argument("--apply", action="store_true", help="Actually update profile YAML files  (default: dry run)")
    return parser


def load_config(config_path: Path) -> dict[str, object]:
    config = deepcopy(DEFAULT_CONFIG)
    if not config_path.exists():
        return config

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read config.yaml. Install curator/requirements.txt") from exc

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config root must be a mapping: {config_path}")

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
    issues = validate_system_mappings(mappings)
    print_system_mappings(mappings, issues)
    return issues


def run_profiles(config: dict[str, object]):
    mappings = _load_configured_mappings(config)
    profiles = _load_configured_profiles(config)
    validation = validate_profiles(profiles, mappings)
    print_profiles(profiles, mappings, validation)
    return validation


def run_profile(config: dict[str, object], name: str):
    mappings = _load_configured_mappings(config)
    profiles = _load_configured_profiles(config)
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise KeyError(f"Unknown profile '{name}'. Available profiles: {available}")
    issues = validate_profile(profiles[name], mappings)
    print_profile_detail(name, profiles[name], mappings, issues)
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


def run_build(config: dict[str, object], name: str, *, systems: list[str] | None = None, year_from: int | None = None, year_to: int | None = None, execute: bool, rebuild: bool, yes: bool, mame_versions: list[str] | None = None):
    plan = _create_configured_export_plan(config, name, systems=systems, year_from=year_from, year_to=year_to, mame_versions=mame_versions)
    print_export_plan(plan)
    result = execute_export_plan(plan, dry_run=not execute, rebuild=rebuild, yes=yes)
    print_export_result(result)
    return result


def run_sync(config: dict[str, object], name: str, *, systems: list[str] | None = None, year_from: int | None = None, year_to: int | None = None, execute: bool, prune: bool, yes: bool, mame_versions: list[str] | None = None):
    plan = _create_configured_export_plan(config, name, systems=systems, year_from=year_from, year_to=year_to, mame_versions=mame_versions)
    print_export_plan(plan)
    result = execute_export_plan(plan, dry_run=not execute, prune=prune, yes=yes)
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

    issues = validate_profile(profiles[name], mappings)
    errors = [issue.message for issue in issues if issue.level == "error"]
    if errors:
        raise ValueError("; ".join(errors))

    # Apply CLI year overrides on top of profile selection without mutating the
    # cached profile dict (deepcopy already done in load_profiles).
    profile = profiles[name]
    if year_from is not None or year_to is not None:
        profile = {**profile, "selection": {**(profile.get("selection") or {}), **({} if year_from is None else {"year_from": year_from}), **({} if year_to is None else {"year_to": year_to})}}

    return create_export_plan(
        _resolve_config_path(config, str(database_path)),
        name,
        profile,
        mappings,
        _resolve_config_path(config, str(exports_path)),
        roms_root=_resolve_config_path(config, str(roms_path)) if roms_path else None,
        systems_filter=systems,
        mame_versions=mame_versions,
    )


def _load_configured_mappings(config: dict[str, object]) -> dict[str, dict[str, object]]:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")
    mappings_path = paths.get("mappings") or Path(__file__).with_name("mappings") / "systems.yaml"
    return load_system_mappings(_resolve_config_path(config, str(mappings_path)))


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
