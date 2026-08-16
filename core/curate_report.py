"""Curation report — surface low-value ROMs by size using cached ROMM/IGDB metadata.

Read-only.  Helps shrink a large archive by ranking candidates for removal
(unidentified by ROMM, or identified with a low IGDB rating) largest-first per
system, so review effort goes to the files that actually recover space.

Not an auto-delete list: IGDB rating reflects crowd opinion, not the user's,
and will happily flag niche/import-only titles alongside genuine filler.
"""

from __future__ import annotations

from pathlib import Path

from .database import InventoryDatabase
from .reporting import (
    _format_bytes,
    _format_percent,
    _print_heading,
    _print_line,
    _print_table,
    _reports_root,
    _save_report,
)

try:
    from rich.console import Console
except ImportError:  # pragma: no cover
    Console = None


# Arcade/MAME systems have essentially no ROMM/IGDB coverage per-file (MAME
# romsets aren't catalogued individually), so "no ROMM match" there is noise,
# not a low-value signal.  Excluded from the default (--systems-less) run;
# pass --systems explicitly to include them anyway.
_LOW_SIGNAL_SYSTEMS: frozenset[str] = frozenset({
    "arcade", "mame", "mame2003-plus", "cps1", "cps2", "cps3",
    "neogeo", "naomi", "naomi2", "atomiswave",
})

# Matches the fs_stem derivation used in database.py's iter_roms_by_systems.
_STEM_SQL = """
    CASE
        WHEN SUBSTR(r.filename, -5, 1) = '.' THEN SUBSTR(r.filename, 1, LENGTH(r.filename) - 5)
        WHEN SUBSTR(r.filename, -4, 1) = '.' THEN SUBSTR(r.filename, 1, LENGTH(r.filename) - 4)
        WHEN SUBSTR(r.filename, -3, 1) = '.' THEN SUBSTR(r.filename, 1, LENGTH(r.filename) - 3)
        WHEN SUBSTR(r.filename, -2, 1) = '.' THEN SUBSTR(r.filename, 1, LENGTH(r.filename) - 2)
        ELSE r.filename
    END
"""


def run_curate_report(
    config: dict[str, object],
    *,
    mappings: dict[str, dict[str, object]] | None = None,
    systems: list[str] | None = None,
    rating_threshold: float = 50.0,
    limit_per_system: int = 25,
    min_size_mb: float = 0.0,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Invalid config: expected 'paths' mapping")

    database_path = Path(str(paths["database"])).expanduser()
    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    # Folder-based systems (switch, scummvm, dos, windows, megacd) store a
    # game as multiple files under one subfolder — per-file size/ROMM lookup
    # doesn't map cleanly to "one game", so they're excluded for now.
    folder_based: frozenset[str] = frozenset(
        s for s, meta in (mappings or {}).items()
        if isinstance(meta, dict) and meta.get("folder_based")
    )

    min_size_bytes = int(min_size_mb * 1024 * 1024)
    reports_root = _reports_root(paths)
    console = Console(record=reports_root is not None) if Console else None

    with InventoryDatabase(database_path) as db:
        db.initialize()

        if systems:
            requested = set(systems)
            target_systems = sorted(requested - folder_based)
            skipped_folder = sorted(requested & folder_based)
            low_signal_included = sorted(requested & _LOW_SIGNAL_SYSTEMS)
            low_signal_skipped: list[str] = []
        else:
            all_systems = {row["system"] for row in db.fetch_all("SELECT DISTINCT system FROM roms")}
            target_systems = sorted(all_systems - folder_based - _LOW_SIGNAL_SYSTEMS)
            skipped_folder = sorted(all_systems & folder_based)
            low_signal_included = []
            low_signal_skipped = sorted(all_systems & _LOW_SIGNAL_SYSTEMS)

        _print_heading(f"Curate Report: {database_path}", console)
        if low_signal_skipped:
            _print_line(
                f"Skipped (low ROMM/IGDB coverage, pass --systems to force): "
                f"{', '.join(low_signal_skipped)}",
                console,
            )
        if low_signal_included:
            _print_line(
                f"Note: low ROMM/IGDB coverage for {', '.join(low_signal_included)} "
                f"— expect mostly 'no ROMM match' noise there",
                console,
            )
        if skipped_folder:
            _print_line(
                f"Skipped (folder-based systems not yet supported): {', '.join(skipped_folder)}",
                console,
            )

        if not target_systems:
            _print_line("No systems to analyse.", console)
            return

        placeholders = ",".join("?" * len(target_systems))
        rows = db.fetch_all(
            f"""
            SELECT r.system, r.title, r.filename, r.relative_path, r.size,
                   rr.romm_id, rr.is_identified, rr.total_rating
            FROM roms r
            LEFT JOIN romm_roms rr
                ON rr.canonical_system = r.system
                AND (rr.fs_name = r.filename OR rr.fs_stem = {_STEM_SQL})
            WHERE r.system IN ({placeholders})
              AND r.size >= ?
            """,
            tuple(target_systems) + (min_size_bytes,),
        )

        candidates: dict[str, list[tuple[str, object]]] = {}
        system_totals: dict[str, tuple[int, int]] = {}

        for row in rows:
            sys_name = row["system"]
            tf, ts = system_totals.get(sys_name, (0, 0))
            system_totals[sys_name] = (tf + 1, ts + int(row["size"] or 0))

            reason = _classify(row, rating_threshold)
            if reason is not None:
                candidates.setdefault(sys_name, []).append((reason, row))

        grand_candidate_count = 0
        grand_candidate_size = 0
        grand_total_size = 0

        for sys_name in sorted(candidates):
            items = sorted(candidates[sys_name], key=lambda pair: int(pair[1]["size"] or 0), reverse=True)
            total_files, total_size = system_totals.get(sys_name, (0, 0))
            cand_count = len(items)
            cand_size = sum(int(row["size"] or 0) for _, row in items)

            grand_candidate_count += cand_count
            grand_candidate_size += cand_size
            grand_total_size += total_size

            _print_table(
                f"{sys_name}  —  {cand_count}/{total_files} candidate(s), "
                f"{_format_bytes(cand_size)} ({_format_percent(cand_size, total_size)} of system size)",
                ["Size", "Reason", "Title", "File"],
                [
                    (_format_bytes(int(row["size"] or 0)), reason, row["title"], row["filename"])
                    for reason, row in items[:limit_per_system]
                ],
                console,
            )
            if cand_count > limit_per_system:
                _print_line(
                    f"  … {cand_count - limit_per_system} more not shown (raise --limit to see them)",
                    console,
                )

        _print_line("", console)
        _print_line(
            f"Total candidates: {grand_candidate_count} file(s), "
            f"{_format_bytes(grand_candidate_size)} potential savings "
            f"({_format_percent(grand_candidate_size, grand_total_size)} of scanned systems)",
            console,
        )
        _print_line(
            "Review list, not an auto-delete list — IGDB rating reflects crowd "
            "opinion, not yours.  Move entries you agree with to the recycle bin "
            "manually, or with a future batch tool.",
            console,
        )

    _save_report(console, reports_root, "curate-report")


def _classify(row, rating_threshold: float) -> str | None:
    """Return a short reason string if this ROM is a curation candidate, else None."""
    if row["romm_id"] is None:
        return "no ROMM match"

    is_identified = row["is_identified"]
    if is_identified is not None and not is_identified:
        return "unidentified"

    total_rating = row["total_rating"]
    # total_rating == 0 is ROMM's "no votes yet" placeholder — not a real low score.
    if total_rating is not None and 0 < total_rating < rating_threshold:
        return f"low rating ({total_rating:.0f})"

    return None
