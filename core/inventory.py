"""Inventory orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from .database import InventoryDatabase
from .parser import parse_filename
from .scanner import iter_rom_files

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
except ImportError:  # pragma: no cover - exercised only without optional deps
    Console = None
    Progress = None
    SpinnerColumn = None
    TextColumn = None
    TimeElapsedColumn = None


@dataclass(frozen=True)
class InventorySummary:
    scanned: int
    added_or_updated: int
    skipped_unchanged: int
    removed_stale: int
    database_path: Path


BATCH_SIZE = 1000


def run_inventory(
    config: dict[str, object],
    systems: list[str] | None = None,
    mappings: dict[str, dict[str, object]] | None = None,
) -> InventorySummary:
    paths = config.get("paths", {})
    scan_config = config.get("scan", {})
    if not isinstance(paths, dict) or not isinstance(scan_config, dict):
        raise ValueError("Invalid config: expected 'paths' and 'scan' mappings")

    roms_root = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()
    incremental = bool(scan_config.get("incremental", True))
    ignore_hidden = bool(scan_config.get("ignore_hidden", True))
    follow_symlinks = bool(scan_config.get("follow_symlinks", False))
    excluded_extensions = _load_excluded_extensions(config)

    # Build nas_path → canonical lookup from mappings (supports subpath nas entries).
    nas_to_canonical: dict[str, str] = {}
    if mappings:
        for canonical, meta in mappings.items():
            if isinstance(meta, dict) and meta.get("nas"):
                nas_to_canonical[str(meta["nas"])] = canonical

    if systems:
        scan_label = ", ".join(f"{roms_root}/{_nas_path(s, mappings)}" for s in systems)
    else:
        scan_label = str(roms_root)

    console = Console() if Console else None
    if console:
        console.print(f"Scanning [bold]{scan_label}[/bold]")
        console.print(f"Database [bold]{database_path}[/bold]")
    else:
        print(f"Scanning {scan_label}")
        print(f"Database {database_path}")

    scanned = 0
    added_or_updated = 0
    skipped_unchanged = 0
    removed_stale = 0
    scan_timestamp = int(time.time())

    # Each system is scanned and stale-pruned independently so that systems not
    # included in this run are never touched in the database.
    scan_targets: list[str | None] = systems if systems else [None]

    with InventoryDatabase(database_path) as db:
        db.initialize()

        for canonical in scan_targets:
            if canonical:
                nas = _nas_path(canonical, mappings)
                # Skip subdirs that are themselves canonical system roots nested
                # under this system's NAS folder (e.g. skip mame2003-plus/ when
                # scanning arcade/ so those files are inventoried separately).
                skip = _child_subdirs(nas, nas_to_canonical)
                known_scan_keys = db.get_scan_keys(system=canonical) if incremental else {}
            else:
                nas = None
                skip = frozenset()
                known_scan_keys = db.get_scan_keys() if incremental else {}

            task_label = f"Walking {nas or 'ROM archive'}"
            if Progress:
                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    TextColumn("{task.completed} files"),
                    TimeElapsedColumn(),
                    console=console,
                )
                with progress:
                    task_id = progress.add_task(task_label, total=None)
                    for record in iter_rom_files(
                        roms_root,
                        system=nas,
                        canonical_system=canonical,
                        nas_to_canonical=nas_to_canonical if not canonical else None,
                        ignore_hidden=ignore_hidden,
                        follow_symlinks=follow_symlinks,
                        excluded_extensions=excluded_extensions,
                        skip_subdirs=skip,
                    ):
                        scanned, added_or_updated, skipped_unchanged = _handle_record(
                            db,
                            record,
                            known_scan_keys,
                            incremental,
                            scan_timestamp,
                            scanned,
                            added_or_updated,
                            skipped_unchanged,
                        )
                        if scanned % BATCH_SIZE == 0:
                            db.commit()
                        progress.advance(task_id)
            else:
                for record in iter_rom_files(
                    roms_root,
                    system=nas,
                    canonical_system=canonical,
                    nas_to_canonical=nas_to_canonical if not canonical else None,
                    ignore_hidden=ignore_hidden,
                    follow_symlinks=follow_symlinks,
                    excluded_extensions=excluded_extensions,
                    skip_subdirs=skip,
                ):
                    scanned, added_or_updated, skipped_unchanged = _handle_record(
                        db,
                        record,
                        known_scan_keys,
                        incremental,
                        scan_timestamp,
                        scanned,
                        added_or_updated,
                        skipped_unchanged,
                    )
                    if scanned % BATCH_SIZE == 0:
                        db.commit()

            if canonical:
                removed_stale += db.remove_stale(scan_timestamp, system=canonical)
            else:
                removed_stale += db.remove_stale(scan_timestamp)

        db.commit()

    summary = InventorySummary(
        scanned=scanned,
        added_or_updated=added_or_updated,
        skipped_unchanged=skipped_unchanged,
        removed_stale=removed_stale,
        database_path=database_path,
    )
    _print_summary(summary, console)
    return summary


def _handle_record(
    db: InventoryDatabase,
    record,
    known_scan_keys: dict[str, str],
    incremental: bool,
    scan_timestamp: int,
    scanned: int,
    added_or_updated: int,
    skipped_unchanged: int,
) -> tuple[int, int, int]:
    scanned += 1
    db.mark_seen(record.path, record.scan_key, scan_timestamp)

    if incremental and known_scan_keys.get(record.path) == record.scan_key:
        skipped_unchanged += 1
        return scanned, added_or_updated, skipped_unchanged

    parsed = parse_filename(record.filename)
    db.upsert_rom(
        {
            "system": record.system,
            "title": parsed.title,
            "filename": record.filename,
            "extension": record.extension,
            "path": record.path,
            "relative_path": record.relative_path,
            "size": record.size,
            "modified": record.modified,
            "region": parsed.region,
            "revision": parsed.revision,
            "disc": parsed.disc,
            "is_beta": int(parsed.is_beta),
            "is_proto": int(parsed.is_proto),
            "is_translation": int(parsed.is_translation),
            "is_hack": int(parsed.is_hack),
            "scan_key": record.scan_key,
        }
    )
    added_or_updated += 1
    return scanned, added_or_updated, skipped_unchanged


def _nas_path(canonical: str, mappings: dict[str, dict[str, object]] | None) -> str:
    """Return the NAS folder path for a canonical system name."""
    if mappings and canonical in mappings:
        meta = mappings[canonical]
        if isinstance(meta, dict) and meta.get("nas"):
            return str(meta["nas"])
    return canonical


def _child_subdirs(nas_path: str, nas_to_canonical: dict[str, str]) -> frozenset[str]:
    """Return immediate subdirectory names that are themselves canonical NAS roots.

    When scanning ``arcade/``, this returns ``{"mame2003-plus"}`` so the walker
    skips that subfolder — those files are inventoried separately as their own
    canonical system.
    """
    prefix = nas_path.rstrip("/") + "/"
    subdirs: set[str] = set()
    for other_nas in nas_to_canonical:
        if other_nas.startswith(prefix):
            remainder = other_nas[len(prefix):]
            first = remainder.split("/")[0]
            if first:
                subdirs.add(first)
    return frozenset(subdirs)


def _load_excluded_extensions(config: dict[str, object]) -> frozenset[str]:
    """Load extension exclusion list from the path referenced in scan.excluded_extensions."""
    scan_config = config.get("scan") or {}
    excl_path_raw = scan_config.get("excluded_extensions")
    if not excl_path_raw:
        return frozenset()

    config_dir = Path(str(config.get("_config_dir", Path(__file__).parent)))
    excl_path = Path(str(excl_path_raw))
    if not excl_path.is_absolute():
        excl_path = config_dir / excl_path

    if not excl_path.exists():
        print(f"Warning: excluded_extensions file not found: {excl_path}")
        return frozenset()

    try:
        import yaml
    except ImportError:
        return frozenset()

    with excl_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    exts: set[str] = set()
    for category_val in data.values():
        if isinstance(category_val, list):
            for ext in category_val:
                if ext:
                    exts.add(str(ext).lower().lstrip("."))
    return frozenset(exts)


def _print_summary(summary: InventorySummary, console) -> None:
    lines = [
        "Inventory complete",
        f"Scanned: {summary.scanned}",
        f"Added/updated: {summary.added_or_updated}",
        f"Unchanged: {summary.skipped_unchanged}",
        f"Removed stale: {summary.removed_stale}",
    ]
    if console:
        console.print("\n".join(lines), style="green")
    else:
        print("\n".join(lines))
