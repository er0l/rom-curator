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


def run_inventory(config: dict[str, object]) -> InventorySummary:
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

    console = Console() if Console else None
    if console:
        console.print(f"Scanning [bold]{roms_root}[/bold]")
        console.print(f"Database [bold]{database_path}[/bold]")
    else:
        print(f"Scanning {roms_root}")
        print(f"Database {database_path}")

    scanned = 0
    added_or_updated = 0
    skipped_unchanged = 0
    scan_timestamp = int(time.time())

    with InventoryDatabase(database_path) as db:
        db.initialize()
        known_scan_keys = db.get_scan_keys() if incremental else {}

        if Progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TextColumn("{task.completed} files"),
                TimeElapsedColumn(),
                console=console,
            )
            with progress:
                task_id = progress.add_task("Walking ROM archive", total=None)
                for record in iter_rom_files(
                    roms_root,
                    ignore_hidden=ignore_hidden,
                    follow_symlinks=follow_symlinks,
                    excluded_extensions=excluded_extensions,
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
                ignore_hidden=ignore_hidden,
                follow_symlinks=follow_symlinks,
                excluded_extensions=excluded_extensions,
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

        removed_stale = db.remove_stale(scan_timestamp)
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
