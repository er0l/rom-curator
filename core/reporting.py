"""Inventory reporting."""

from __future__ import annotations

import datetime
from pathlib import Path

from .database import InventoryDatabase

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


def run_report(
    config: dict[str, object],
    mappings: dict[str, dict[str, object]] | None = None,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Invalid config: expected 'paths' mapping")

    database_path = Path(str(paths["database"])).expanduser()
    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    # Systems where each game is a subfolder — file counts are misleading for these.
    folder_based: frozenset[str] = frozenset(
        s for s, meta in (mappings or {}).items()
        if isinstance(meta, dict) and meta.get("folder_based")
    )

    reports_root = _reports_root(paths)
    console = Console(record=reports_root is not None) if Console else None

    with InventoryDatabase(database_path) as db:
        db.initialize()

        # For folder_based systems count unique game subfolders, not raw files.
        # A "game" is the immediate subdirectory under the system folder; flat
        # files at the system root each count as one game.
        # SQL: extract the path segment between the first and second '/'.
        # depth-2 path  (system/file)        → game key = relative_path (unique per file)
        # depth-3+ path (system/folder/...)  → game key = system + '/' + subfolder
        total_files = db.fetch_scalar("SELECT COUNT(*) FROM roms")
        total_games = db.fetch_scalar(
            """
            SELECT COUNT(DISTINCT
                CASE WHEN relative_path LIKE '%/%/%'
                     THEN SUBSTR(relative_path, 1,
                          INSTR(SUBSTR(relative_path, INSTR(relative_path, '/') + 1), '/')
                          + INSTR(relative_path, '/'))
                     ELSE relative_path
                END)
            FROM roms
            """
        )
        total_size = db.fetch_scalar("SELECT SUM(size) FROM roms")

        _print_heading(f"Inventory Report: {database_path}", console)
        if total_games != total_files:
            _print_line(f"Games: {total_games}  (files on disk: {total_files})", console)
        else:
            _print_line(f"Files: {total_files}", console)
        _print_line(f"Total size: {_format_bytes(total_size)}", console)

        # Systems by size — show game count for folder_based, file count otherwise.
        systems_rows = db.fetch_all(
            """
            SELECT system,
                COUNT(*) AS files,
                COUNT(DISTINCT
                    CASE WHEN relative_path LIKE '%/%/%'
                         THEN SUBSTR(relative_path, 1,
                              INSTR(SUBSTR(relative_path, INSTR(relative_path, '/') + 1), '/')
                              + INSTR(relative_path, '/'))
                         ELSE relative_path
                    END) AS games,
                SUM(size) AS size
            FROM roms
            GROUP BY system
            ORDER BY size DESC
            LIMIT 25
            """
        )
        _print_table(
            "Systems by Size",
            ["System", "Games", "Files", "Size"],
            [
                (
                    row["system"],
                    row["games"] if row["system"] in folder_based else "—",
                    row["files"],
                    _format_bytes(row["size"]),
                )
                for row in systems_rows
            ],
            console,
        )

        _print_table(
            "Top Extensions",
            ["Extension", "Files", "Size"],
            [
                (row["extension"] or "(none)", row["files"], _format_bytes(row["size"]))
                for row in db.fetch_all(
                    """
                    SELECT extension, COUNT(*) AS files, SUM(size) AS size
                    FROM roms
                    GROUP BY extension
                    ORDER BY files DESC
                    LIMIT 25
                    """
                )
            ],
            console,
        )

        _print_table(
            "Largest ROMs",
            ["System", "Size", "Relative Path"],
            [
                (row["system"], _format_bytes(row["size"]), row["relative_path"])
                for row in db.fetch_all(
                    """
                    SELECT system, size, relative_path
                    FROM roms
                    ORDER BY size DESC
                    LIMIT 20
                    """
                )
            ],
            console,
        )

        # Region breakdown — exclude subfolder file contents from folder_based systems
        # (their filenames don't carry region tags so they inflate the Unknown bucket).
        if folder_based:
            fb_list = ",".join(f"'{s}'" for s in sorted(folder_based))
            region_filter = (
                f"WHERE system NOT IN ({fb_list}) "
                f"OR relative_path NOT LIKE '%/%/%'"
            )
        else:
            region_filter = ""
        region_total = db.fetch_scalar(
            f"SELECT COUNT(*) FROM roms {region_filter}"
        )
        _print_table(
            "Region Breakdown",
            ["Region", "Files", "Percent"],
            [
                (
                    row["region"] or "Unknown",
                    row["files"],
                    _format_percent(row["files"], region_total),
                )
                for row in db.fetch_all(
                    f"""
                    SELECT COALESCE(region, 'Unknown') AS region, COUNT(*) AS files
                    FROM roms
                    {region_filter}
                    GROUP BY COALESCE(region, 'Unknown')
                    ORDER BY files DESC
                    """
                )
            ],
            console,
        )

        # Possible duplicates — exclude subfolder contents of folder_based systems
        # since generic filenames like '0001' repeat across many game folders.
        if folder_based:
            dup_filter = (
                f"AND (system NOT IN ({fb_list}) "
                f"OR relative_path NOT LIKE '%/%/%')"
            )
        else:
            dup_filter = ""
        _print_table(
            "Possible Duplicates",
            ["System", "Title", "Files"],
            [
                (row["system"], row["title"], row["files"])
                for row in db.fetch_all(
                    f"""
                    SELECT system, title, COUNT(*) AS files
                    FROM roms
                    WHERE 1=1 {dup_filter}
                    GROUP BY system, title
                    HAVING COUNT(*) > 1
                    ORDER BY files DESC, system, title
                    LIMIT 25
                    """
                )
            ],
            console,
        )

    _save_report(console, reports_root, "report")


def run_arcade_analyze(config: dict[str, object]) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Invalid config: expected 'paths' mapping")

    database_path = Path(str(paths["database"])).expanduser()
    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    reports_root = _reports_root(paths)
    console = Console(record=reports_root is not None) if Console else None

    with InventoryDatabase(database_path) as db:
        db.initialize()
        total = db.fetch_scalar("SELECT COUNT(*) FROM roms WHERE system = 'arcade'")
        total_size = db.fetch_scalar("SELECT SUM(size) FROM roms WHERE system = 'arcade'")
        chd_files = db.fetch_scalar("SELECT COUNT(*) FROM roms WHERE system = 'arcade' AND extension = 'chd'")

        _print_heading(f"Arcade Analysis: {database_path}", console)
        _print_line(f"Arcade files: {total}", console)
        _print_line(f"Arcade size: {_format_bytes(total_size)}", console)
        _print_line(f"CHD files: {chd_files}", console)
        _print_table(
            "Arcade Extensions",
            ["Extension", "Files", "Size"],
            [
                (row["extension"] or "(none)", row["files"], _format_bytes(row["size"]))
                for row in db.fetch_all(
                    """
                    SELECT extension, COUNT(*) AS files, SUM(size) AS size
                    FROM roms
                    WHERE system = 'arcade'
                    GROUP BY extension
                    ORDER BY files DESC
                    """
                )
            ],
            console,
        )

    _save_report(console, reports_root, "arcade-analyze")


def _reports_root(paths: dict) -> Path | None:
    value = paths.get("reports")
    return Path(str(value)).expanduser() if value else None


def _save_report(console, reports_root: Path | None, prefix: str) -> None:
    if not reports_root or not console:
        return
    reports_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = reports_root / f"{prefix}_{timestamp}.txt"
    console.save_text(str(report_file))
    print(f"\nReport saved: {report_file}")


def _print_heading(text: str, console) -> None:
    if console:
        console.rule(f"[bold]{text}[/bold]")
    else:
        print(f"\n{text}\n{'=' * len(text)}")


def _print_line(text: str, console) -> None:
    if console:
        console.print(text)
    else:
        print(text)


def _print_table(title: str, columns: list[str], rows: list[tuple[object, ...]], console) -> None:
    if console and Table:
        table = Table(title=title)
        for column in columns:
            table.add_column(column)
        for row in rows:
            table.add_row(*(str(value) for value in row))
        console.print(table)
        return

    print(f"\n{title}")
    print("-" * len(title))
    print(" | ".join(columns))
    for row in rows:
        print(" | ".join(str(value) for value in row))


def _format_bytes(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if size < 1024 or unit == "P":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}P"


def _format_percent(count: int, total: int) -> str:
    if not total:
        return "0.0%"
    return f"{(count / total) * 100:.1f}%"
