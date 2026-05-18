"""Inventory reporting."""

from __future__ import annotations

import datetime
from pathlib import Path

from .database import InventoryDatabase

try:
    from rich.console import Console
    from rich.markup import escape as markup_escape
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    markup_escape = None  # type: ignore[assignment]
    Table = None


def run_report(
    config: dict[str, object],
    mappings: dict[str, dict[str, object]] | None = None,
    systems: list[str] | None = None,
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
    # Systems whose subfolder files are raw game data without region tags
    # (e.g. scummvm data files, megacd audio tracks).  These are excluded from
    # region breakdown and duplicate detection.  Systems like switch are
    # folder_based but their depth-3 files ARE the ROM with proper naming, so
    # they do NOT set subfolder_exclude.
    subfolder_exclude: frozenset[str] = frozenset(
        s for s, meta in (mappings or {}).items()
        if isinstance(meta, dict) and meta.get("subfolder_exclude")
    )

    # Build a reusable SQL fragment + params tuple that scopes every query to
    # the requested systems when --systems is given.
    if systems:
        placeholders = ",".join("?" * len(systems))
        sys_where = f"WHERE system IN ({placeholders})"   # standalone WHERE
        sys_and   = f"AND system IN ({placeholders})"     # appended AND
        sys_params: tuple = tuple(systems)
    else:
        sys_where = ""
        sys_and   = ""
        sys_params = ()

    reports_root = _reports_root(paths)
    console = Console(record=reports_root is not None) if Console else None

    with InventoryDatabase(database_path) as db:
        db.initialize()

        # --- Headline totals ------------------------------------------------
        # For folder_based systems count unique game subfolders, not raw files.
        # A "game" is the immediate subdirectory under the system folder; flat
        # files at the system root each count as one game.
        # SQL: extract the path segment between the first and second '/'.
        # depth-2 path  (system/file)        → game key = relative_path (unique per file)
        # depth-3+ path (system/folder/...)  → game key = system + '/' + subfolder
        total_files = db.fetch_scalar(
            f"SELECT COUNT(*) FROM roms {sys_where}", sys_params
        )
        total_games = db.fetch_scalar(
            f"""
            SELECT COUNT(DISTINCT
                CASE WHEN relative_path LIKE '%/%/%'
                     THEN SUBSTR(relative_path, 1,
                          INSTR(SUBSTR(relative_path, INSTR(relative_path, '/') + 1), '/')
                          + INSTR(relative_path, '/'))
                     ELSE relative_path
                END)
            FROM roms {sys_where}
            """,
            sys_params,
        )
        total_size = db.fetch_scalar(
            f"SELECT SUM(size) FROM roms {sys_where}", sys_params
        )

        if systems:
            heading = f"Report ({', '.join(systems)}): {database_path}"
        else:
            heading = f"Inventory Report: {database_path}"
        _print_heading(heading, console)
        _print_line(f"Games: {total_games}", console)
        if total_games != total_files:
            _print_line(f"Files on disk: {total_files}", console)
        _print_line(f"Total size: {_format_bytes(total_size)}", console)

        # --- Systems by size ------------------------------------------------
        # When filtered to a specific set of systems show all of them (no LIMIT
        # truncation); for the full-archive view keep the top-25 cap.
        limit_clause = "" if systems else "LIMIT 25"
        systems_rows = db.fetch_all(
            f"""
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
            {sys_where}
            GROUP BY system
            ORDER BY size DESC
            {limit_clause}
            """,
            sys_params,
        )
        # When only one system is requested this table would be a single row —
        # skip it; the headline already shows the same numbers.
        if not systems or len(systems) > 1:
            _print_table(
                "Systems by Size",
                ["System", "Games", "Files", "Size"],
                [
                    (
                        row["system"],
                        row["games"],   # unique subfolders for folder_based; = files for flat systems
                        row["files"],
                        _format_bytes(row["size"]),
                    )
                    for row in systems_rows
                ],
                console,
            )

        # --- Top extensions -------------------------------------------------
        _print_table(
            "Top Extensions",
            ["Extension", "Files", "Size"],
            [
                (row["extension"] or "(none)", row["files"], _format_bytes(row["size"]))
                for row in db.fetch_all(
                    f"""
                    SELECT extension, COUNT(*) AS files, SUM(size) AS size
                    FROM roms
                    {sys_where}
                    GROUP BY extension
                    ORDER BY files DESC
                    LIMIT 25
                    """,
                    sys_params,
                )
            ],
            console,
        )

        # --- Largest ROMs ---------------------------------------------------
        largest_cols = ["Size", "Relative Path"] if systems and len(systems) == 1 else ["System", "Size", "Relative Path"]
        largest_rows_raw = db.fetch_all(
            f"""
            SELECT system, size, relative_path
            FROM roms
            {sys_where}
            ORDER BY size DESC
            LIMIT 20
            """,
            sys_params,
        )
        largest_rows = (
            [(_format_bytes(r["size"]), r["relative_path"]) for r in largest_rows_raw]
            if systems and len(systems) == 1
            else [(r["system"], _format_bytes(r["size"]), r["relative_path"]) for r in largest_rows_raw]
        )
        _print_table("Largest ROMs", largest_cols, largest_rows, console)

        # --- Region breakdown -----------------------------------------------
        # Two levels of subfolder exclusion:
        #
        # 1. subfolder_exclude systems (scummvm, dos, windows, megacd):
        #    depth-3+ files are raw game data with no region tags — exclude all.
        #    Pattern: relative_path LIKE '%/%/%'  (2+ slashes → depth ≥ 3)
        #
        # 2. Other folder_based systems (switch):
        #    depth-3 files ARE the ROM (e.g. switch/GameName/game.nsp) — include.
        #    depth-4+ files are supplementary packages (updates/) — exclude.
        #    Pattern: relative_path LIKE '%/%/%/%'  (3+ slashes → depth ≥ 4)
        active_se  = subfolder_exclude & set(systems) if systems else subfolder_exclude
        active_fb_only = (folder_based - subfolder_exclude) & set(systems) if systems else (folder_based - subfolder_exclude)

        clauses: list[str] = []
        if active_se:
            se_list = ",".join(f"'{s}'" for s in sorted(active_se))
            clauses.append(f"(system NOT IN ({se_list}) OR relative_path NOT LIKE '%/%/%')")
        if active_fb_only:
            fb_only_list = ",".join(f"'{s}'" for s in sorted(active_fb_only))
            clauses.append(f"(system NOT IN ({fb_only_list}) OR relative_path NOT LIKE '%/%/%/%')")

        combined_clause = " AND ".join(clauses)
        if sys_where and combined_clause:
            region_filter = f"{sys_where} AND {combined_clause}"
        elif combined_clause:
            region_filter = f"WHERE {combined_clause}"
        else:
            region_filter = sys_where  # may be empty or "WHERE system IN (...)"

        region_total = db.fetch_scalar(
            f"SELECT COUNT(*) FROM roms {region_filter}", sys_params
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
                    """,
                    sys_params,
                )
            ],
            console,
        )

        # --- Possible duplicates --------------------------------------------
        # Same two-level exclusion as region breakdown.
        dup_clauses = [f"AND {c}" for c in clauses]
        dup_extra = " ".join(dup_clauses)

        dup_limit = "" if systems else "LIMIT 25"
        _print_table(
            "Possible Duplicates",
            ["System", "Title", "Files"],
            [
                (row["system"], row["title"], row["files"])
                for row in db.fetch_all(
                    f"""
                    SELECT system, title, COUNT(*) AS files
                    FROM roms
                    WHERE 1=1 {sys_and} {dup_extra}
                    GROUP BY system, title
                    HAVING COUNT(*) > 1
                      AND COUNT(DISTINCT COALESCE(disc, '')) < COUNT(*)
                    ORDER BY files DESC, system, title
                    {dup_limit}
                    """,
                    sys_params,
                )
            ],
            console,
        )

    report_prefix = f"report-{'_'.join(systems)}" if systems else "report"
    _save_report(console, reports_root, report_prefix)


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
