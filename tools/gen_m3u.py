"""Generate .m3u playlist files for multi-disc ROM games.

Scans the inventory database for ROMs that have a disc tag (Disc 1, Disc 2,
Side A, Side B, …) and writes one .m3u file per multi-disc game in the
system's root folder.

Dry-run is the default.  Pass --execute to actually create the files.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.database import InventoryDatabase

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


def run_gen_m3u(
    config: dict[str, object],
    *,
    mappings: dict[str, dict[str, object]] | None = None,
    systems: list[str] | None = None,
    execute: bool = False,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    database_path = Path(str(paths["database"])).expanduser()
    roms_root = Path(str(paths["roms"])).expanduser()

    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    # Folder-based systems store each game in a subfolder.  Generating a flat
    # .m3u alongside disc files that live inside those subfolders would place
    # the playlist at the wrong level, so we skip them here.
    folder_based: frozenset[str] = frozenset(
        s for s, meta in (mappings or {}).items()
        if isinstance(meta, dict) and meta.get("folder_based")
    )

    console = Console() if Console else None

    with InventoryDatabase(database_path) as db:
        db.initialize()

        if systems:
            placeholders = ",".join("?" * len(systems))
            rows = db.fetch_all(
                f"""
                SELECT system, title, disc, filename, relative_path, region
                FROM roms
                WHERE disc IS NOT NULL AND system IN ({placeholders})
                ORDER BY system, title, disc
                """,
                tuple(systems),
            )
        else:
            rows = db.fetch_all(
                """
                SELECT system, title, disc, filename, relative_path, region
                FROM roms
                WHERE disc IS NOT NULL
                ORDER BY system, title, disc
                """
            )

    # Group by (system, lowercase-title) so that minor naming differences
    # between disc files don't split a game into separate playlists.
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        sys = str(row["system"])
        if sys in folder_based:
            continue  # flat .m3u not appropriate for subfolder-based systems
        key = (sys, str(row["title"]).lower())
        groups.setdefault(key, []).append(row)

    # Build the list of (m3u_path, display_title, ordered_filenames).
    plans: list[tuple[Path, str, list[str]]] = []
    for (sys, _lower_title), disc_rows in sorted(groups.items(), key=lambda x: x[0]):
        if len(disc_rows) < 2:
            continue  # single-disc — no playlist needed

        # Sort discs: numeric identifiers first (Disc 1 < Disc 2), then alpha
        # (Side A < Side B).  Unknown formats fall to the end.
        sorted_rows = sorted(disc_rows, key=lambda r: _disc_sort_key(str(r["disc"])))

        # Use the canonical title from the first (best-sorted) disc row.
        display_title = str(sorted_rows[0]["title"])
        m3u_path = roms_root / sys / f"{display_title}.m3u"
        filenames = [str(r["filename"]) for r in sorted_rows]
        plans.append((m3u_path, display_title, filenames))

    if not plans:
        _print(console, "No multi-disc games found.")
        return

    mode_label = "DRY RUN" if not execute else "EXECUTE"
    _print(console, f"\nM3U Generator — {mode_label}")
    _print(console, f"Found {len(plans)} multi-disc game(s)\n")

    for m3u_path, display_title, filenames in plans:
        rel = m3u_path.relative_to(roms_root)
        status = _m3u_status(m3u_path, filenames)
        _print(console, f"  {status:<9s}  {rel}")
        for fname in filenames:
            _print(console, f"             {fname}")

    if not execute:
        _print(
            console,
            f"\nDRY RUN — {len(plans)} .m3u file(s) would be written. Pass --execute to create.",
        )
        return

    created = updated = skipped = errors = 0
    for m3u_path, _title, filenames in plans:
        status = _m3u_status(m3u_path, filenames)
        if status == "UNCHANGED":
            skipped += 1
            continue
        try:
            m3u_path.parent.mkdir(parents=True, exist_ok=True)
            m3u_path.write_text("\n".join(filenames) + "\n", encoding="utf-8")
            if status == "UPDATE":
                updated += 1
            else:
                created += 1
        except Exception as exc:
            errors += 1
            _print(console, f"  ERROR  {m3u_path.name}: {exc}", style="red")

    style = "red" if errors else "green"
    _print(console, f"\nDone — created: {created}  updated: {updated}  unchanged: {skipped}  errors: {errors}", style=style)


def _m3u_status(m3u_path: Path, filenames: list[str]) -> str:
    """Return 'CREATE', 'UPDATE', or 'UNCHANGED' for a planned .m3u write."""
    if not m3u_path.exists():
        return "CREATE"
    try:
        existing = m3u_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "UPDATE"
    return "UNCHANGED" if existing == filenames else "UPDATE"


def _disc_sort_key(disc: str) -> tuple:
    """Return a sort key for a disc string such as '(Disc 1)', '(Side A)', '(Tape 2)'.

    Numeric identifiers sort before alpha; within each group, sort by value.
    """
    # Match the identifying token: a number or a single letter just before ')'
    m = re.search(r"(\d+|[A-Za-z])\s*\)", disc)
    if m:
        val = m.group(1)
        try:
            return (0, int(val), "")
        except ValueError:
            return (1, 0, val.upper())
    return (2, 0, disc)


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)
