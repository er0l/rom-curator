"""Hardlink export planning and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import shutil

from .compat import CompatList, load_compat_lists, passes_compat
from .database import InventoryDatabase
from .mappings import get_preferred_alias
from .profiles import selected_systems

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


@dataclass(frozen=True)
class ExportPlanItem:
    source: Path
    destination: Path
    system: str
    target_system: str
    title: str
    size: int


@dataclass
class ExportSystemSummary:
    seen: int = 0
    selected: int = 0
    selected_size: int = 0
    skipped_region: int = 0
    skipped_beta: int = 0
    skipped_proto: int = 0
    skipped_hack: int = 0
    skipped_translation: int = 0
    skipped_rating: int = 0
    skipped_unidentified: int = 0
    duplicate_regions_removed: int = 0
    arcade_clones_removed: int = 0
    skipped_non_game: int = 0
    skipped_controls: int = 0
    skipped_compat: int = 0
    skipped_year: int = 0
    capped: int = 0
    no_target_alias: int = 0


@dataclass
class ExportPlan:
    profile_name: str
    target: str
    export_root: Path
    items: list[ExportPlanItem] = field(default_factory=list)
    summaries: dict[str, ExportSystemSummary] = field(default_factory=dict)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.items)


@dataclass(frozen=True)
class ExportResult:
    planned: int
    linked: int
    skipped_existing: int
    conflicts: int
    pruned: int
    dry_run: bool
    export_root: Path


def create_export_plan(
    database_path: str | Path,
    profile_name: str,
    profile: dict[str, object],
    mappings: dict[str, dict[str, object]],
    exports_root: str | Path,
    roms_root: str | Path | None = None,
    systems_filter: list[str] | None = None,
    mame_versions: list[str] | None = None,
    layouts: dict[str, dict[str, list[str]]] | None = None,
    compat_lists: dict[str, CompatList] | None = None,
) -> ExportPlan:
    target = str(profile.get("target"))
    export_root = Path(exports_root).expanduser() / profile_name
    _roms_root = Path(roms_root).expanduser() if roms_root else None
    plan = ExportPlan(profile_name=profile_name, target=target, export_root=export_root)
    systems = selected_systems(profile, mappings)
    if systems_filter:
        unknown = sorted(set(systems_filter) - set(systems))
        if unknown:
            print(f"Warning: systems not in profile and will be ignored: {', '.join(unknown)}")
        systems = [s for s in systems if s in set(systems_filter)]
    preferred_regions = _as_string_list(profile.get("preferred_region"))
    max_games = profile.get("max_games_per_system")
    selection = profile.get("selection") if isinstance(profile.get("selection"), dict) else {}

    systems_set = set(systems)
    arcade_dedupe = bool(selection.get("arcade_dedupe", True))
    arcade_skip_non_playable = bool(selection.get("arcade_skip_non_playable", True))
    arcade_exclude_controls: frozenset[str] = frozenset(
        str(c) for c in (selection.get("arcade_exclude_controls") or [])
    )
    # mame_versions: restrict arcade ROMs to machines present in these versioned romsets.
    # CLI --mame-versions overrides the profile's selection.mame_versions.
    _mame_versions: list[str] | None = mame_versions or _as_string_list(
        selection.get("mame_versions")
    ) or None
    # Compat list settings — loaded per profile chip, applied per system.
    compat_chip: str | None = str(selection.get("compat_chip")) if selection.get("compat_chip") else None
    compat_min_playability: str = str(selection.get("compat_min_playability") or "Ok")
    compat_include_unlisted: bool = bool(selection.get("compat_include_unlisted", True))
    # Systems where unlisted ROMs are excluded (only confirmed-tested games exported).
    # Overrides compat_include_unlisted=true for specific systems.
    compat_unlisted_exclude: frozenset[str] = frozenset(
        str(s) for s in (selection.get("compat_unlisted_exclude") or [])
    )
    # compat_lists can be passed in (pre-loaded by caller) or will be empty.
    _compat_lists: dict[str, CompatList] = compat_lists or {}

    # Folder-based systems store each game as a subfolder (e.g. scummvm, dos, windows).
    # The export unit is the whole subfolder; all files within it are hardlinked together.
    folder_based_systems: frozenset[str] = frozenset(
        s for s in systems if mappings.get(s, {}).get("folder_based", False)
    )
    # Track unique subfolder names seen per folder-based system (for the Seen counter).
    folder_seen: dict[str, set[str]] = {s: set() for s in folder_based_systems}

    grouped: dict[str, dict[tuple[str, str | None], list[object]]] = {system: {} for system in systems}
    with InventoryDatabase(database_path) as db:
        db.initialize()
        for row in db.iter_roms_by_systems(systems, mame_versions=_mame_versions):
            # For arcade ROMs with a classified sub-system, route to that sub-system
            # if it's in the profile; otherwise fall back to 'arcade'.
            # Example: arcade_system='mame' has no standalone canonical system entry
            # in systems.yaml, so those ROMs fall back to the 'arcade' canonical system
            # and are exported to whatever folder 'arcade' maps to (e.g. 'mame' in Batocera).
            effective = _effective_system(row)
            if effective not in systems_set:
                if row["system"] == "arcade" and "arcade" in systems_set:
                    effective = "arcade"
                else:
                    continue
            summary = plan.summaries.setdefault(effective, ExportSystemSummary())

            if effective in folder_based_systems:
                # Group by the immediate subfolder under the system root.
                # For flat files at the system root, use the filename as the key.
                group_key = _folder_group_key(row)
                folder_name = group_key[0]
                if folder_name not in folder_seen[effective]:
                    folder_seen[effective].add(folder_name)
                    summary.seen += 1  # count games (subfolders), not individual files
            else:
                summary.seen += 1
                group_key = _group_key(row, arcade_dedupe=arcade_dedupe)
            grouped.setdefault(effective, {}).setdefault(group_key, []).append(row)

    for system in systems:
        target_alias = get_preferred_alias(layouts or {}, system, target)
        summary = plan.summaries.setdefault(system, ExportSystemSummary())
        if not target_alias:
            summary.no_target_alias += summary.seen
            continue

        is_arcade_system = system in _ARCADE_SUBSYSTEMS or system == "arcade"
        is_folder_based = system in folder_based_systems
        selected_for_system = 0
        title_groups = grouped.get(system, {})
        compat = _compat_lists.get(system) if compat_chip else None
        include_unlisted = compat_include_unlisted and system not in compat_unlisted_exclude

        for rows in title_groups.values():
            if is_folder_based:
                # Folder-based game: hardlink every file in the subfolder as one unit.
                # Region/beta/hack/year filters don't apply at the individual-file level
                # for multi-file game installs, but compat filtering applies per game.
                if not passes_compat(compat, rows[0], compat_min_playability, include_unlisted):
                    summary.skipped_compat += 1
                    continue
                if isinstance(max_games, int) and selected_for_system >= max_games:
                    summary.capped += 1
                    continue
                game_size = 0
                for row in rows:
                    if _roms_root is not None:
                        source = _roms_root / str(row["relative_path"])
                    else:
                        source = Path(str(row["path"]))
                    destination = _destination_for(
                        export_root, target_alias,
                        str(row["system"]), str(row["relative_path"]),
                    )
                    file_size = int(row["size"])
                    plan.items.append(ExportPlanItem(
                        source=source,
                        destination=destination,
                        system=system,
                        target_system=target_alias,
                        title=str(_folder_group_key(row)[0]),
                        size=file_size,
                    ))
                    game_size += file_size
                selected_for_system += 1
                summary.selected += 1
                summary.selected_size += game_size
                continue

            candidates = []
            for row in rows:
                # Arcade-specific: skip BIOS/device/mechanical ROMs before other filters
                if is_arcade_system and arcade_skip_non_playable and _is_arcade_non_game(row):
                    summary.skipped_non_game += 1
                    continue
                # Arcade-specific: skip games requiring unavailable control hardware.
                # When mame_control_types is NULL (MAME data not imported or game
                # unrecognised), the check is skipped so no games are falsely excluded.
                if is_arcade_system and arcade_exclude_controls and _needs_excluded_control(row, arcade_exclude_controls):
                    summary.skipped_controls += 1
                    continue
                if not passes_compat(compat, row, compat_min_playability, include_unlisted):
                    summary.skipped_compat += 1
                    continue
                skip_reason = _skip_reason(row, preferred_regions, selection)
                if skip_reason:
                    _record_skip(summary, skip_reason)
                    continue
                candidates.append(row)

            if not candidates:
                continue

            if is_arcade_system and arcade_dedupe:
                chosen = _choose_arcade_preferred(candidates)
                # All candidates beyond the chosen one are redundant clones of the same parent
                summary.arcade_clones_removed += max(0, len(candidates) - 1)
            else:
                chosen = _choose_preferred(candidates, preferred_regions)
            summary.duplicate_regions_removed += max(0, len(candidates) - 1) if not (is_arcade_system and arcade_dedupe) else 0
            if isinstance(max_games, int) and selected_for_system >= max_games:
                summary.capped += 1
                continue

            # Prefer roms_root + relative_path so --roms override takes effect;
            # fall back to the absolute path stored in the DB.
            if _roms_root is not None:
                source = _roms_root / str(chosen["relative_path"])
            else:
                source = Path(str(chosen["path"]))
            # Use the original NAS system folder (e.g. 'arcade') for path stripping,
            # not the effective sub-system name.
            destination = _destination_for(export_root, target_alias, str(chosen["system"]), str(chosen["relative_path"]))
            item_size = int(chosen["size"])
            plan.items.append(
                ExportPlanItem(
                    source=source,
                    destination=destination,
                    system=system,
                    target_system=target_alias,
                    title=str(chosen["title"]),
                    size=item_size,
                )
            )
            selected_for_system += 1
            summary.selected += 1
            summary.selected_size += item_size

    return plan


def execute_export_plan(
    plan: ExportPlan,
    *,
    dry_run: bool = True,
    rebuild: bool = False,
    prune: bool = False,
    yes: bool = False,
) -> ExportResult:
    if (rebuild or prune) and not yes:
        raise ValueError("--rebuild and --prune require --yes")

    if dry_run:
        return ExportResult(
            planned=len(plan.items),
            linked=0,
            skipped_existing=0,
            conflicts=0,
            pruned=0,
            dry_run=True,
            export_root=plan.export_root,
        )

    if rebuild and plan.export_root.exists():
        shutil.rmtree(plan.export_root)

    linked = 0
    skipped_existing = 0
    conflicts = 0
    pruned = 0
    planned_destinations = {item.destination.resolve() for item in plan.items}

    for item in plan.items:
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        if item.destination.exists():
            if _same_file(item.source, item.destination):
                skipped_existing += 1
                continue
            conflicts += 1
            continue
        os.link(item.source, item.destination)
        linked += 1

    if prune and plan.export_root.exists():
        for path in sorted(plan.export_root.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
                continue
            if path.resolve() not in planned_destinations:
                path.unlink()
                pruned += 1

    return ExportResult(
        planned=len(plan.items),
        linked=linked,
        skipped_existing=skipped_existing,
        conflicts=conflicts,
        pruned=pruned,
        dry_run=False,
        export_root=plan.export_root,
    )


def print_export_plan(plan: ExportPlan) -> None:
    console = Console() if Console else None
    rows = [
        (
            system,
            str(summary.seen),
            str(summary.selected),
            _format_bytes(summary.selected_size),
            str(summary.skipped_region),
            str(summary.skipped_beta),
            str(summary.skipped_proto),
            str(summary.skipped_hack),
            str(summary.skipped_rating),
            str(summary.skipped_unidentified),
            str(summary.skipped_non_game),
            str(summary.skipped_controls),
            str(summary.skipped_compat),
            str(summary.skipped_year),
            str(summary.capped),
            str(summary.duplicate_regions_removed),
            str(summary.arcade_clones_removed),
        )
        for system, summary in sorted(plan.summaries.items())
        if summary.seen or summary.selected
    ]

    if console and Table:
        console.print(f"Profile: [bold]{plan.profile_name}[/bold]")
        console.print(f"Target: [bold]{plan.target}[/bold]")
        console.print(f"Export root: [bold]{plan.export_root}[/bold]")
        table = Table(title="Export Plan")
        for column in ("System", "Seen", "Selected", "Size", "Region", "Beta", "Proto", "Hack", "Rating", "Unidentified", "Non-game", "Controls", "Compat", "Year", "Cap", "Dupes", "Clones"):
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        console.print(table)
        console.print(f"Planned hardlinks: {len(plan.items)}")
        console.print(f"Logical size: {_format_bytes(plan.total_size)}")
        return

    print(f"Profile: {plan.profile_name}")
    print(f"Target: {plan.target}")
    print(f"Export root: {plan.export_root}")
    print("System | Seen | Selected | Size | Region | Beta | Proto | Hack | Rating | Unidentified | Non-game | Controls | Compat | Year | Cap | Dupes | Clones")
    for row in rows:
        print(" | ".join(row))
    print(f"Planned hardlinks: {len(plan.items)}")
    print(f"Logical size: {_format_bytes(plan.total_size)}")


def print_export_result(result: ExportResult) -> None:
    mode = "dry run" if result.dry_run else "executed"
    print(f"Build {mode}: {result.export_root}")
    print(f"Planned: {result.planned}")
    print(f"Linked: {result.linked}")
    print(f"Skipped existing: {result.skipped_existing}")
    print(f"Conflicts: {result.conflicts}")
    print(f"Pruned: {result.pruned}")


def _skip_reason(row, preferred_regions: list[str], selection: dict[str, object]) -> str | None:
    region = row["region"]
    if preferred_regions and region and region not in preferred_regions:
        return "region"
    if row["is_beta"] and not bool(selection.get("include_beta", False)):
        return "beta"
    if row["is_proto"] and not bool(selection.get("include_proto", False)):
        return "proto"
    if row["is_hack"] and not bool(selection.get("include_hacks", False)):
        return "hack"
    if row["is_translation"] and not bool(selection.get("include_translations", True)):
        return "translation"

    # Year filter — applied when a release year can be determined from either
    # ROMM metadata or (for arcade) the MAME machine record.  Games with no
    # year data are never penalised.
    year_from = selection.get("year_from")
    year_to = selection.get("year_to")
    if year_from is not None or year_to is not None:
        game_year = _game_year(row)
        if game_year is not None:
            if year_from is not None and game_year < int(year_from):
                return "year"
            if year_to is not None and game_year > int(year_to):
                return "year"

    # ROMM-based filters — only applied when a matching romm_roms record exists.
    # NULL fields mean no ROMM data for this file; those games are never penalised.
    min_rating = selection.get("min_rating")
    if min_rating is not None:
        total_rating = row["total_rating"]
        # total_rating == 0 is ROMM's "no votes yet" placeholder — treat as unrated, not zero.
        if total_rating is not None and total_rating > 0 and total_rating < float(min_rating):
            return "rating"

    if bool(selection.get("identified_only", False)):
        is_identified = row["is_identified"]
        if is_identified is not None and not is_identified:
            return "unidentified"

    return None


def _record_skip(summary: ExportSystemSummary, reason: str) -> None:
    if reason == "region":
        summary.skipped_region += 1
    elif reason == "beta":
        summary.skipped_beta += 1
    elif reason == "proto":
        summary.skipped_proto += 1
    elif reason == "hack":
        summary.skipped_hack += 1
    elif reason == "translation":
        summary.skipped_translation += 1
    elif reason == "rating":
        summary.skipped_rating += 1
    elif reason == "unidentified":
        summary.skipped_unidentified += 1
    elif reason == "year":
        summary.skipped_year += 1


def _game_year(row) -> int | None:
    """Return the best available release year for a ROM row, or None.

    Priority:
      1. ROMM year (reliable integer from IGDB/ROMM metadata)
      2. MAME year (arcade machines — stored as TEXT, may contain '?' markers
         such as '1991?' or '19??'; partial years are ignored)

    Returns None when no reliable year is available, so the caller can
    skip the year filter rather than falsely excluding the game.
    """
    romm_year = row["romm_year"]
    if romm_year and int(romm_year) > 0:
        return int(romm_year)
    mame_year = row["mame_year"]
    if mame_year:
        clean = str(mame_year).replace("?", "").strip()
        if clean.isdigit() and len(clean) == 4:
            return int(clean)
    return None


def _folder_group_key(row) -> tuple[str, None]:
    """For folder-based systems, group all files that share the same immediate
    subfolder under the system root.

    Layout examples:
      scummvm/Monkey Island/mi.000   → group key: "Monkey Island"
      scummvm/+Start ScummVM.sh      → group key: "+Start ScummVM.sh"  (flat file)
      windows/Jedi Knight/JEDI_1.iso → group key: "Jedi Knight"
      dos/King's Quest.zip           → group key: "King's Quest.zip"   (flat file)
      dos/HoMM2/HEROES2.EXE          → group key: "HoMM2"

    Files sitting directly in the system root (no subfolder) are each their own
    group so they are exported as standalone items.
    """
    parts = Path(str(row["relative_path"])).parts
    # parts[0] = system folder, parts[1] = game subfolder or flat filename
    # Only use parts[1] as a game folder when there are deeper files (parts[2+])
    if len(parts) >= 3:
        return (parts[1], None)
    return (str(row["filename"]), None)


def _group_key(row, *, arcade_dedupe: bool) -> tuple[str, str | None]:
    """Return the grouping key for a ROM row.

    For arcade ROMs with deduplication enabled, all clones of the same parent
    are collapsed into one group keyed by the parent machine name.  For
    everything else (and when arcade_dedupe is False) the key is the parsed
    title plus optional disc tag, matching the classic cartridge behaviour.
    """
    if row["system"] in _ARCADE_SUBSYSTEMS | {"arcade"} and arcade_dedupe:
        parent = str(row["mame_cloneof"]) if row["mame_cloneof"] else str(row["title"])
        return (parent, None)
    return (str(row["title"]), row["disc"] if row["disc"] else None)


def _is_arcade_non_game(row) -> bool:
    """Return True for MAME mechanical ROMs (slot machines, pachinko, pinball).

    BIOS and device ROMs are intentionally NOT excluded here — many playable
    games depend on them at runtime (e.g. neogeo.zip for all Neo Geo games)
    and must be present in the export folder alongside the game ROMs.
    """
    return bool(row["mame_ismechanical"])


def _needs_excluded_control(row, excluded: frozenset[str]) -> bool:
    """Return True if this arcade ROM requires a control type the device lacks.

    control_types in the DB is stored as a semicolon-separated string of MAME
    control type names (e.g. "joy;wheel" or "trackball").  When the field is
    NULL (MAME XML not yet imported from a full mame -listxml source, or game
    unrecognised), the function returns False so that unknown games are never
    falsely excluded.

    Common MAME control types:
      joy, doublejoy  — digital joystick(s)
      stick           — analogue stick
      wheel           — steering wheel
      trackball       — trackball
      spinner         — spinner / dial
      paddle          — paddle (Arkanoid-style)
      lightgun        — light gun
      mouse           — mouse
      pedal           — foot pedal
    """
    raw = row["mame_control_types"]
    if not raw:
        return False
    game_controls = frozenset(raw.split(";"))
    return bool(game_controls & excluded)


def _choose_arcade_preferred(candidates):
    """Pick the single best arcade ROM from a parent-grouped candidate list.

    Preference order:
      1. Parent ROM (cloneof IS NULL) — the canonical machine entry
      2. Alphabetical by filename as a stable tiebreaker among clones
    """
    return sorted(
        candidates,
        key=lambda row: (
            0 if not row["mame_cloneof"] else 1,
            str(row["filename"]),
        ),
    )[0]


# Arcade sub-system names produced by arcade.py classify_sourcefile().
# Used to decide whether arcade-specific filtering applies to a system.
_ARCADE_SUBSYSTEMS = frozenset({"cps1", "cps2", "cps3", "neogeo", "naomi", "naomi2", "atomiswave", "mame", "mame2003-plus"})


def _choose_preferred(rows, preferred_regions: list[str]):
    region_rank = {region: index for index, region in enumerate(preferred_regions)}
    return sorted(
        rows,
        key=lambda row: (
            region_rank.get(row["region"], len(region_rank)),
            int(row["is_beta"]),
            int(row["is_proto"]),
            int(row["is_hack"]),
            str(row["filename"]),
        ),
    )[0]


def _effective_system(row) -> str:
    """For arcade ROMs with a classified sub-system, return arcade_system; else return system."""
    if row["system"] == "arcade" and row["arcade_system"]:
        return str(row["arcade_system"])
    return str(row["system"])


def _destination_for(export_root: Path, target_alias: str, system: str, relative_path: str) -> Path:
    parts = Path(relative_path).parts
    remainder = parts[1:] if parts and parts[0] == system else parts
    return export_root / target_alias / Path(*remainder)


def _same_file(source: Path, destination: Path) -> bool:
    try:
        return source.stat().st_ino == destination.stat().st_ino and source.stat().st_dev == destination.stat().st_dev
    except OSError:
        return False


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _format_bytes(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if size < 1024 or unit == "P":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}P"
