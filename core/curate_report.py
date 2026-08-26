"""Curation report — surface low-value or top-rated ROMs using cached ROMM/IGDB metadata.

Read-only.  Default mode helps shrink a large archive by ranking removal
candidates (unidentified by ROMM, or identified with a low IGDB rating)
largest-first per system.  ``--top`` flips this around to surface the
best-rated games instead, sorted highest-rated first.

Neither mode is an auto-delete/auto-keep list: IGDB rating reflects crowd
opinion, not the user's, and will happily flag niche/import-only titles
alongside genuine filler (or miss a beloved cult game that never got votes).
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
# romsets aren't catalogued individually), so results there are mostly noise
# in either mode.  Excluded from the default (--systems-less) run; pass
# --systems explicitly to include them anyway.
_LOW_SIGNAL_SYSTEMS: frozenset[str] = frozenset({
    "arcade", "mame", "mame2003-plus", "cps1", "cps2", "cps3",
    "neogeo", "naomi", "naomi2", "atomiswave",
})

# Companion/cuesheet/playlist files that describe or accompany a primary disc
# image — never an independent game on their own.  Same list dedup_roms.py
# uses to keep them out of duplicate-group logic.  Excluded here so a
# multi-disc game's .m3u (and any .cue/.gdi sidecar) doesn't show up as its
# own near-zero-size "candidate" alongside the actual disc files.
_COMPANION_EXTENSIONS: frozenset[str] = frozenset({
    "cue", "mds", "gdi", "sub", "sbi", "m3u",
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
    rating_threshold: float | None = None,
    limit_per_system: int = 25,
    min_size_mb: float = 0.0,
    top: bool = False,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Invalid config: expected 'paths' mapping")

    database_path = Path(str(paths["database"])).expanduser()
    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    # Default threshold differs by mode: below 50 is "low value"; at/above 80
    # is "top rated".  Either can be overridden with --rating-threshold.
    threshold = rating_threshold if rating_threshold is not None else (80.0 if top else 50.0)

    # Folder-based systems (switch, scummvm, dos, windows, megacd, dreamcast,
    # saturn) store a game as multiple files under one subfolder.  They're
    # supported here by grouping files per subfolder (mirrors exporter.py's
    # _folder_group_key) and matching ROMM by folder/title name rather than
    # raw filename — ROMM's fs_name for these systems is the game folder
    # name, not a per-file name (see gen_gamelist.py's `rr.fs_stem = r.title`).
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
            target_systems = sorted(requested)
            low_signal_included = sorted(requested & _LOW_SIGNAL_SYSTEMS)
            low_signal_skipped: list[str] = []
        else:
            all_systems = {row["system"] for row in db.fetch_all("SELECT DISTINCT system FROM roms")}
            target_systems = sorted(all_systems - _LOW_SIGNAL_SYSTEMS)
            low_signal_included = []
            low_signal_skipped = sorted(all_systems & _LOW_SIGNAL_SYSTEMS)

        heading = "Top Rated Report" if top else "Curate Report"
        _print_heading(f"{heading}: {database_path}", console)
        if low_signal_skipped:
            _print_line(
                f"Skipped (low ROMM/IGDB coverage, pass --systems to force): "
                f"{', '.join(low_signal_skipped)}",
                console,
            )
        if low_signal_included:
            _print_line(
                f"Note: low ROMM/IGDB coverage for {', '.join(low_signal_included)} "
                f"— expect few or no results there",
                console,
            )

        flat_systems = [s for s in target_systems if s not in folder_based]
        folder_systems = [s for s in target_systems if s in folder_based]
        if folder_systems:
            _print_line(
                f"Folder-based systems grouped by game folder "
                f"(size = ROM + updates/DLC combined): {', '.join(folder_systems)}",
                console,
            )

        if not target_systems:
            _print_line("No systems to analyse.", console)
            return

        # Each candidate tuple: (reason, title, file_label, size, sort_value)
        # sort_value is size in low-value mode, rating in top mode.
        candidates: dict[str, list[tuple[str, str, str, int, float]]] = {}
        system_totals: dict[str, tuple[int, int]] = {}

        if flat_systems:
            _collect_flat_candidates(db, flat_systems, threshold, min_size_bytes, top, candidates, system_totals)

        for sys_name in folder_systems:
            _collect_folder_candidates(db, sys_name, threshold, min_size_bytes, top, candidates, system_totals)

        grand_candidate_count = 0
        grand_candidate_size = 0
        grand_total_size = 0

        sort_index = 4 if top else 3  # top: sort by rating; low-value: sort by size
        for sys_name in sorted(candidates):
            items = sorted(candidates[sys_name], key=lambda t: t[sort_index], reverse=True)
            total_files, total_size = system_totals.get(sys_name, (0, 0))
            cand_count = len(items)
            cand_size = sum(t[3] for t in items)

            grand_candidate_count += cand_count
            grand_candidate_size += cand_size
            grand_total_size += total_size

            noun = "top-rated" if top else "candidate"
            _print_table(
                f"{sys_name}  —  {cand_count}/{total_files} {noun}(s), "
                f"{_format_bytes(cand_size)} ({_format_percent(cand_size, total_size)} of system size)",
                ["Size", "Reason", "Title", "File"],
                [
                    (_format_bytes(size), reason, title, file_label)
                    for reason, title, file_label, size, _sort in items[:limit_per_system]
                ],
                console,
            )
            if cand_count > limit_per_system:
                _print_line(
                    f"  … {cand_count - limit_per_system} more not shown (raise --limit to see them)",
                    console,
                )

        _print_line("", console)
        if top:
            _print_line(
                f"Total top-rated: {grand_candidate_count} item(s), "
                f"{_format_bytes(grand_candidate_size)} "
                f"({_format_percent(grand_candidate_size, grand_total_size)} of scanned systems)",
                console,
            )
            _print_line(
                "Games rated ≥ this threshold by IGDB — a starting point for what's "
                "worth keeping/prioritising, not a verdict on everything else.",
                console,
            )
        else:
            _print_line(
                f"Total candidates: {grand_candidate_count} item(s), "
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

    _save_report(console, reports_root, "top-rated-report" if top else "curate-report")


def _collect_flat_candidates(
    db: InventoryDatabase,
    flat_systems: list[str],
    threshold: float,
    min_size_bytes: int,
    top: bool,
    candidates: dict[str, list[tuple[str, str, str, int, float]]],
    system_totals: dict[str, tuple[int, int]],
) -> None:
    """One-file-per-game systems: join roms → romm_roms by filename/stem, then
    group by (system, title) so a multi-disc game's several .iso/.chd/.cso
    files collapse into one item with a summed size, instead of one row per
    disc.  Companion files (.m3u, .cue, .gdi, ...) are excluded entirely —
    they aren't game data and would otherwise show up as their own
    near-zero-size "candidate".

    ROMM matches each disc file independently (and even the .m3u, when
    present, as yet another separate romm_roms record) but reports the same
    rating for all of them — so the first match found in the group is used
    as the representative for classification.
    """
    placeholders = ",".join("?" * len(flat_systems))
    rows = db.fetch_all(
        f"""
        SELECT r.system, r.title, r.filename, r.extension, r.size,
               rr.romm_id, rr.is_identified, rr.total_rating
        FROM roms r
        LEFT JOIN romm_roms rr
            ON rr.canonical_system = r.system
            AND (rr.fs_name = r.filename OR rr.fs_stem = {_STEM_SQL})
        WHERE r.system IN ({placeholders})
        """,
        tuple(flat_systems),
    )

    groups: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        if str(row["extension"] or "").lower() in _COMPANION_EXTENSIONS:
            continue
        key = (str(row["system"]), str(row["title"]))
        g = groups.setdefault(key, {"size": 0, "filenames": [], "info": None})
        g["size"] = int(g["size"]) + int(row["size"] or 0)  # type: ignore[arg-type]
        g["filenames"].append(str(row["filename"]))  # type: ignore[union-attr]
        if g["info"] is None and row["romm_id"] is not None:
            g["info"] = (row["romm_id"], row["is_identified"], row["total_rating"])

    for (sys_name, title), g in groups.items():
        size = int(g["size"])  # type: ignore[arg-type]
        if size < min_size_bytes:
            continue

        tf, ts = system_totals.get(sys_name, (0, 0))
        system_totals[sys_name] = (tf + 1, ts + size)

        filenames = g["filenames"]  # type: ignore[assignment]
        file_label = filenames[0] if len(filenames) == 1 else f"{len(filenames)} files (multi-disc)"

        romm_id, is_identified, total_rating = g["info"] if g["info"] else (None, None, None)  # type: ignore[misc]
        result = _classify(romm_id, is_identified, total_rating, threshold, top)
        if result is not None:
            reason, sort_value = result
            candidates.setdefault(sys_name, []).append((reason, title, file_label, size, sort_value))


def _collect_folder_candidates(
    db: InventoryDatabase,
    system: str,
    threshold: float,
    min_size_bytes: int,
    top: bool,
    candidates: dict[str, list[tuple[str, str, str, int, float]]],
    system_totals: dict[str, tuple[int, int]],
) -> None:
    """Folder-based systems: group files per game subfolder, match ROMM by folder/title name.

    Mirrors exporter.py's _folder_group_key (depth-3+ paths group by the
    subfolder name; flat root files are their own group) and gen_gamelist.py's
    ROMM match, which uses the folder/title name — ROMM stores fs_name as the
    game folder name for these systems, not a per-file filename.
    """
    rows = db.fetch_all(
        "SELECT title, filename, relative_path, size FROM roms WHERE system = ?",
        (system,),
    )
    if not rows:
        return

    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        parts = Path(str(row["relative_path"])).parts
        key = parts[1] if len(parts) >= 3 else str(row["filename"])
        g = groups.setdefault(key, {"title": str(row["title"]), "size": 0, "filenames": []})
        g["size"] = int(g["size"]) + int(row["size"] or 0)
        g["filenames"].append(str(row["filename"]))  # type: ignore[union-attr]

    romm_rows = db.fetch_all(
        "SELECT fs_name, fs_stem, romm_id, is_identified, total_rating FROM romm_roms WHERE canonical_system = ?",
        (system,),
    )
    romm_index: dict[str, tuple] = {}
    for r in romm_rows:
        info = (r["romm_id"], r["is_identified"], r["total_rating"])
        if r["fs_name"]:
            romm_index.setdefault(str(r["fs_name"]), info)
        if r["fs_stem"]:
            romm_index.setdefault(str(r["fs_stem"]), info)

    total_files = 0
    total_size = 0
    for key, g in groups.items():
        total_files += 1
        size = int(g["size"])  # type: ignore[arg-type]
        total_size += size
        if size < min_size_bytes:
            continue

        title = str(g["title"])
        info = romm_index.get(key) or romm_index.get(title)
        if info is None:
            for fn in g["filenames"]:  # type: ignore[union-attr]
                info = romm_index.get(fn) or romm_index.get(_file_stem(fn))
                if info is not None:
                    break

        romm_id, is_identified, total_rating = info if info is not None else (None, None, None)
        result = _classify(romm_id, is_identified, total_rating, threshold, top)
        if result is not None:
            reason, sort_value = result
            candidates.setdefault(system, []).append((reason, title, key, size, sort_value))

    system_totals[system] = (total_files, total_size)


def _classify(
    romm_id: object,
    is_identified: object,
    total_rating: object,
    threshold: float,
    top: bool,
) -> tuple[str, float] | None:
    """Return (reason, sort_value) if this ROM/game matches the requested mode, else None.

    Low-value mode (default): flags ROMs with no ROMM match, ROMM-unidentified
    ROMs, or identified ROMs with a real rating below *threshold*.  The caller
    sorts by size in this mode, not sort_value — it's set to the rating when
    available, 0.0 otherwise, and is unused for ordering.

    Top mode: flags only identified ROMs with a real rating at/above
    *threshold*.  sort_value is the rating, used to sort highest-first.
    """
    if top:
        if romm_id is None:
            return None
        if is_identified is not None and not is_identified:
            return None
        if total_rating is None:
            return None
        rating = float(total_rating)
        # total_rating == 0 is ROMM's "no votes yet" placeholder — never "top".
        if rating <= 0 or rating < threshold:
            return None
        return (f"rated {rating:.0f}", rating)

    if romm_id is None:
        return ("no ROMM match", 0.0)

    if is_identified is not None and not is_identified:
        return ("unidentified", 0.0)

    if total_rating is not None:
        rating = float(total_rating)
        # total_rating == 0 is ROMM's "no votes yet" placeholder — not a real low score.
        if 0 < rating < threshold:
            return (f"low rating ({rating:.0f})", rating)

    return None


def _file_stem(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[:dot] if dot > 0 else filename
