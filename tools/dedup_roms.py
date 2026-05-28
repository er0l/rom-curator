"""Identify and recycle duplicate ROMs using inventory database metadata.

Groups ROMs by (system, title, disc) and picks one winner per group using the
same priority ordering the export engine uses: preferred region first, then
non-beta > non-proto > non-hack, then compressed format, then filename.

Losers are moved to a recycle bin (preserving relative path) so nothing is
permanently deleted without an explicit recovery step.
"""

from __future__ import annotations

import errno
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core.database import InventoryDatabase

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


DEFAULT_PREFERRED_REGIONS = ["USA", "Europe", "Japan"]

# Companion / cuesheet files that describe or accompany a primary disc image.
# These are never independent games and must never be treated as duplicates or
# as candidates in a dedup group — they travel with their primary file.
_COMPANION_EXTENSIONS: frozenset[str] = frozenset({
    ".cue",   # CD cuesheet (companion to .bin)
    ".mds",   # MDF/MDS metadata descriptor (companion to .mdf — same role as .cue)
    ".gdi",   # Dreamcast cuesheet (companion to .bin track files)
    ".sub",   # subchannel data
    ".sbi",   # subchannel information
    ".m3u",   # multi-disc playlist
})

# Words that carry no identifying information and must be ignored when comparing
# game titles for similarity.  Keeping this small — we only strip particles and
# articles that appear in both Japanese romanisations and English titles.
_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from",   # English
    "no", "de", "wo", "ga", "wa", "ni",    # Japanese particles
})


def _title_tokens(title: str) -> frozenset[str]:
    """Return the set of significant lowercase word-tokens from *title*.

    Strips punctuation, lowercases, then discards tokens that are shorter than
    three characters or in _STOP_WORDS.  The result is used to check whether
    two game titles share any meaningful content — a zero intersection is a
    strong signal that they are different games mis-grouped by ROMM.
    """
    words = re.sub(r"[^\w\s]", " ", title.lower()).split()
    return frozenset(
        w for w in words if len(w) >= 3 and w not in _STOP_WORDS
    )


# Detect disc numbers embedded in old No-Intro combined tags like "(NA - Disc 1)"
# or "(Prototype - Disc A)".  Modern naming uses separate tokens: "(USA) (Disc 1)".
# When disc=None but this pattern is present we synthesise a disc key so that
# "Disc 1" and "Disc 2" files are never collapsed into the same dedup group.
_EMBEDDED_DISC_RE = re.compile(
    r"\([^)]*?-\s*Disc\s+([0-9A-Za-z]+)\)",
    re.IGNORECASE,
)

# Prefer compressed / space-efficient formats over raw ROM files when
# region/flags are equal.  Lower rank = higher preference.
#
#   .zip / .7z   — universal compressed archives (most systems)
#   .chd         — compressed disc image (arcade, CD-based systems)
#   .cso         — compressed ISO for PSP (smaller than raw ISO)
#   .pbp         — Sony encrypted archive for PSX-on-PSP
#   .iso         — uncompressed disc image (beats multi-file bin/cue)
#   .bin         — raw disc data track
#   .img         — raw sector image
#   everything else (raw cartridge dumps, etc.) → rank 99
#   (.cue / .gdi and other companion files are excluded before ranking)
_FORMAT_RANK: dict[str, int] = {
    ".zip": 0,
    ".7z":  1,
    ".chd": 2,
    ".cso": 3,
    ".pbp": 4,
    ".iso": 5,
    ".bin": 6,
    ".img": 7,
}


@dataclass(frozen=True)
class DedupPlanItem:
    loser_path: Path
    keeper_path: Path
    loser_rel: str
    keeper_rel: str
    system: str
    title: str
    reason: str
    match_type: str = "title"   # "title" | "romm"


@dataclass
class DedupSummary:
    total_roms: int = 0
    duplicate_groups: int = 0
    files_to_move: int = 0
    moved: int = 0
    errors: int = 0
    dry_run: bool = True
    romm_groups: int = 0               # groups found via ROMM igdb_id cross-title match
    romm_skipped_unclean: int = 0      # ROMM groups skipped because they contain hacks/betas
    romm_skipped_badmap: int = 0       # ROMM groups skipped due to likely bad igdb_id mapping


def run_dedup_roms(
    config: dict[str, object],
    *,
    mappings: dict[str, dict[str, object]] | None = None,
    system: str | None = None,
    preferred_regions: list[str] | None = None,
    execute: bool = False,
    romm_dupes: bool = False,
    romm_max_group: int = 4,
    romm_skip_igdb: frozenset[str] = frozenset(),
) -> DedupSummary:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    database_path = Path(str(paths["database"])).expanduser()
    roms_root = Path(str(paths["roms"])).expanduser()
    recycle_bin = Path(str(paths.get("recycle_bin", "/mnt/storage/recycle_bin"))).expanduser()
    regions = preferred_regions or DEFAULT_PREFERRED_REGIONS
    console = Console() if Console else None

    # Merge CLI-supplied skip list with any igdb_ids listed in config.yaml under
    # dedup.romm_skip_igdb_ids.  Both sets are always active when --romm-dupes is used.
    _dedup_cfg = config.get("dedup") or {}
    _cfg_skip: list = _dedup_cfg.get("romm_skip_igdb_ids") if isinstance(_dedup_cfg, dict) else []  # type: ignore[assignment]
    _merged_skip: frozenset[str] = romm_skip_igdb | frozenset(str(x) for x in (_cfg_skip or []))

    # Systems where each game is a subfolder — individual files inside those
    # subfolders are game data, not standalone ROMs, and must never be deduped.
    folder_based: frozenset[str] = frozenset(
        s for s, meta in (mappings or {}).items()
        if isinstance(meta, dict) and meta.get("folder_based")
    )
    # Subset of folder_based systems whose subfolder files are untagged data
    # (megacd audio tracks, dreamcast track*.bin, scummvm data files, etc.).
    # For these systems, subfolder files are normally excluded from dedup UNLESS
    # their parsed title matches a flat disc image at the system root — that
    # means a CHD exists that supersedes the old multi-file subfolder.
    subfolder_exclude: frozenset[str] = frozenset(
        s for s, meta in (mappings or {}).items()
        if isinstance(meta, dict) and meta.get("subfolder_exclude")
    )

    items, stats = _build_plan(database_path, roms_root, system, regions, folder_based, subfolder_exclude)

    romm_items: list[DedupPlanItem] = []
    romm_stats: dict = {"groups": 0, "skipped_unclean": 0, "skipped_badmap": 0}
    if romm_dupes:
        # Build a set of all relative paths already covered by title-based dedup
        # (both keepers and losers) so ROMM pass doesn't double-process them.
        title_plan_paths: set[str] = {i.loser_rel for i in items} | {i.keeper_rel for i in items}
        romm_items, romm_stats = _build_romm_plan(
            database_path, roms_root, system, regions,
            already_in_title_plan=title_plan_paths,
            folder_based=folder_based,
            subfolder_exclude=subfolder_exclude,
            max_group_size=romm_max_group,
            skip_igdb_ids=_merged_skip,
        )

    all_items = items + romm_items

    summary = DedupSummary(
        total_roms=stats["total_roms"],
        duplicate_groups=stats["groups"],
        files_to_move=len(all_items),
        dry_run=not execute,
        romm_groups=romm_stats["groups"],
        romm_skipped_unclean=romm_stats["skipped_unclean"],
        romm_skipped_badmap=romm_stats["skipped_badmap"],
    )

    _print_header(summary, recycle_bin, regions, console, romm_dupes=romm_dupes)
    _print_plan(items, console)
    if romm_items:
        _print_romm_plan(romm_items, romm_stats, console)

    if not execute:
        _print(
            console,
            f"\nDRY RUN complete — {summary.files_to_move} duplicate(s) in "
            f"{summary.duplicate_groups + summary.romm_groups} group(s) would be recycled. "
            "Pass --execute to proceed.",
            style="bold",
        )
        return summary

    for item in all_items:
        dest = recycle_bin / "roms" / item.loser_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _safe_move(item.loser_path, dest)
            summary.moved += 1
            _print(
                console,
                f"  MOVED  {Path(item.loser_rel).name}  →  recycle bin  (kept: {Path(item.keeper_rel).name})",
                style="green",
            )
        except Exception as exc:
            summary.errors += 1
            _print(console, f"  ERROR  {item.loser_rel}: {exc}", style="red")

    style = "red" if summary.errors else "green"
    _print(console, f"\nDone — moved: {summary.moved}  errors: {summary.errors}", style=style)
    if summary.errors:
        _print(console, "Hint: re-run with sudo if you see permission errors on NFS mounts.")
    _print(
        console,
        "Inventory is now stale — run 'inventory' to rescan the updated archive.",
        style="yellow",
    )

    return summary


def _build_romm_plan(
    database_path: Path,
    roms_root: Path,
    system: str | None,
    preferred_regions: list[str],
    already_in_title_plan: set[str],
    folder_based: frozenset[str] = frozenset(),
    subfolder_exclude: frozenset[str] = frozenset(),
    max_group_size: int = 4,
    skip_igdb_ids: frozenset[str] = frozenset(),
) -> tuple[list[DedupPlanItem], dict]:
    """Build a dedup plan from ROMM igdb_id cross-title grouping.

    This catches games that No-Intro assigns different titles across markets —
    e.g. '90 Minutes - European Prime Goal (Europe)' and
    'J.League Soccer Prime Goal 3 (Japan)' share igdb_id 42663.

    Safety filter: groups that contain any hack / beta / prototype ROM are
    silently skipped.  Such groups usually represent ROMM incorrectly mapping
    ROM-hacks under the same IGDB entry as the original game, not genuine
    regional variants.

    Files already covered by the title-based dedup pass are excluded so the
    two passes never conflict.
    """
    region_rank = {r: i for i, r in enumerate(preferred_regions)}

    with InventoryDatabase(database_path) as db:
        db.initialize()
        if system:
            rows = db.fetch_all(
                """
                SELECT r.system, r.title, r.disc, r.filename, r.path, r.relative_path,
                       r.region, r.is_beta, r.is_proto, r.is_hack,
                       rr.igdb_id, rr.name AS romm_name
                FROM roms r
                JOIN romm_roms rr
                    ON rr.canonical_system = r.system AND rr.fs_name = r.filename
                WHERE rr.igdb_id IS NOT NULL AND rr.igdb_id != ''
                  AND r.system = ?
                ORDER BY r.system, rr.igdb_id, r.filename
                """,
                (system,),
            )
        else:
            rows = db.fetch_all(
                """
                SELECT r.system, r.title, r.disc, r.filename, r.path, r.relative_path,
                       r.region, r.is_beta, r.is_proto, r.is_hack,
                       rr.igdb_id, rr.name AS romm_name
                FROM roms r
                JOIN romm_roms rr
                    ON rr.canonical_system = r.system AND rr.fs_name = r.filename
                WHERE rr.igdb_id IS NOT NULL AND rr.igdb_id != ''
                  AND r.system NOT IN ('arcade', 'mame2003-plus')
                ORDER BY r.system, rr.igdb_id, r.filename
                """
            )

    # Pre-compute flat disc titles for subfolder_exclude systems (same logic as _build_plan).
    _DISC_IMAGE_EXTS: frozenset[str] = frozenset({".chd", ".cdi", ".iso", ".img"})
    flat_disc_titles_romm: dict[str, set[str]] = {}
    for row in rows:
        sys = str(row["system"])
        if sys not in subfolder_exclude:
            continue
        if len(Path(str(row["relative_path"])).parts) != 2:
            continue
        if Path(str(row["filename"])).suffix.lower() in _DISC_IMAGE_EXTS:
            flat_disc_titles_romm.setdefault(sys, set()).add(str(row["title"]))

    # Group by (system, igdb_id, disc).
    # Including disc in the key prevents Disc 1 and Disc 2 of a multi-disc game
    # from being flagged as duplicates of each other — both carry the same igdb_id.
    # Apply the same synthetic-disc logic as _build_plan so that old-style
    # "(NA - Disc 1)" filenames (where disc=None in the DB) are separated correctly.
    groups: dict[tuple[str, str, str | None], list] = {}
    for row in rows:
        rel = str(row["relative_path"])
        # Skip companion/sidecar files — .m3u, .cue, .gdi, etc. are never standalone.
        if Path(str(row["filename"])).suffix.lower() in _COMPANION_EXTENSIONS:
            continue
        # Skip files already handled by the title-based pass.
        if rel in already_in_title_plan:
            continue
        # Apply the same subfolder filter as _build_plan.
        sys   = str(row["system"])
        parts = Path(rel).parts
        if sys in folder_based and len(parts) >= 3:
            if sys in subfolder_exclude:
                if str(row["title"]) not in flat_disc_titles_romm.get(sys, set()):
                    continue
            else:
                if len(parts) >= 4:
                    continue
        igdb = str(row["igdb_id"])
        disc_val = row["disc"]
        if disc_val is None:
            m = _EMBEDDED_DISC_RE.search(str(row["filename"]))
            if m:
                disc_val = f"(Disc {m.group(1)})"  # synthetic — prevents cross-disc grouping
        groups.setdefault((sys, igdb, disc_val), []).append(row)

    items: list[DedupPlanItem] = []
    dup_groups = 0
    skipped_unclean = 0
    skipped_badmap = 0

    for (sys, igdb_id, _disc), group_rows in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1], x[0][2] or "")):
        if len(group_rows) <= 1:
            continue

        # ── Guard 1: group size cap ────────────────────────────────────────────
        # Large groups almost always mean a ROMM bulk-import error where many
        # unrelated games were assigned to the same igdb_id (e.g. 12 different
        # JP Dreamcast games all mapped to igdb_id=130632 "Sentimental Graffiti 2").
        if len(group_rows) > max_group_size:
            skipped_badmap += 1
            continue

        # ── Guard 2: user-supplied igdb_id skip list ───────────────────────────
        # Lets users permanently ignore igdb_ids that ROMM consistently maps
        # incorrectly (sequels collapsed under one id, franchise compilations, etc.)
        if igdb_id in skip_igdb_ids:
            skipped_badmap += 1
            continue

        # ── Guard 3: hack / beta / prototype filter ────────────────────────────
        # Skip groups that contain ROM-hacks or demo releases — ROMM occasionally
        # maps these under the original game's IGDB id.
        if any(int(r["is_hack"]) or int(r["is_beta"]) or int(r["is_proto"]) for r in group_rows):
            skipped_unclean += 1
            continue

        # Sort to establish keeper (best region / format / filename).
        sorted_rows = sorted(
            group_rows,
            key=lambda r: (
                region_rank.get(str(r["region"]) if r["region"] else "", len(region_rank)),
                _format_rank(str(r["filename"])),
                str(r["filename"]),
            ),
        )
        keeper = sorted_rows[0]
        romm_name = str(keeper["romm_name"]) if keeper["romm_name"] else f"igdb:{igdb_id}"

        # ── Guard 4: keeper ↔ ROMM name title overlap ─────────────────────────
        # If the keeper's No-Intro title shares no significant words with the
        # ROMM canonical name, the igdb_id assignment is almost certainly wrong.
        # Example: keeper="Jikkyou Powerful Pro Yakyuu" / ROMM="Sentimental Graffiti 2"
        # → zero overlap → skip.  Legitimate regional variants share the core
        # franchise name (e.g. "Monaco Grand Prix" appears in both titles).
        romm_tokens = _title_tokens(romm_name)
        keep_tokens = _title_tokens(str(keeper["title"]))
        if romm_tokens and keep_tokens and not (romm_tokens & keep_tokens):
            skipped_badmap += 1
            continue

        # ── Guard 5: per-candidate title overlap filter ────────────────────────
        # Drop any MOVE candidate whose No-Intro title shares no significant words
        # with either the keeper title or the ROMM canonical name.
        # Example: keeper="Street Fighter Alpha 3" / ROMM="Street Fighter Alpha 3" /
        #          loser="Morita no Saikyou Reversi" → zero overlap on both → drop.
        # Games with no title data are kept (fail-safe — never drop by accident).
        losers = []
        for loser in sorted_rows[1:]:
            loser_tokens = _title_tokens(str(loser["title"]))
            if loser_tokens and not (loser_tokens & keep_tokens) and not (loser_tokens & romm_tokens):
                continue  # likely a different game mis-mapped to this igdb_id
            losers.append(loser)

        if not losers:
            skipped_badmap += 1
            continue

        dup_groups += 1
        for loser in losers:
            k_region = str(keeper["region"]) if keeper["region"] else "(none)"
            l_region = str(loser["region"]) if loser["region"] else "(none)"
            reason = (
                f"ROMM: region {k_region} beats {l_region}"
                if k_region != l_region
                else "ROMM: same igdb_id, filename sort"
            )
            items.append(DedupPlanItem(
                loser_path=roms_root / str(loser["relative_path"]),
                keeper_path=roms_root / str(keeper["relative_path"]),
                loser_rel=str(loser["relative_path"]),
                keeper_rel=str(keeper["relative_path"]),
                system=str(keeper["system"]),
                title=romm_name,
                reason=reason,
                match_type="romm",
            ))

    return items, {"groups": dup_groups, "skipped_unclean": skipped_unclean, "skipped_badmap": skipped_badmap}


def _build_plan(
    database_path: Path,
    roms_root: Path,
    system: str | None,
    preferred_regions: list[str],
    folder_based: frozenset[str] = frozenset(),
    subfolder_exclude: frozenset[str] = frozenset(),
) -> tuple[list[DedupPlanItem], dict]:
    region_rank = {r: i for i, r in enumerate(preferred_regions)}

    with InventoryDatabase(database_path) as db:
        db.initialize()
        if system:
            rows = db.fetch_all(
                "SELECT system, title, disc, filename, path, relative_path, region, "
                "is_beta, is_proto, is_hack FROM roms WHERE system = ? ORDER BY system, title, filename",
                (system,),
            )
        else:
            rows = db.fetch_all(
                "SELECT system, title, disc, filename, path, relative_path, region, "
                "is_beta, is_proto, is_hack FROM roms ORDER BY system, title, filename"
            )

    # For subfolder_exclude systems (dreamcast, megacd, …) pre-compute the set
    # of titles that have a flat disc image at the system root (depth = 2).
    # A subfolder file whose parsed title appears in this set belongs to a game
    # that has been re-released as a single CHD — it IS a dedup candidate.
    # Subfolder files whose titles are NOT in the set are generic data tracks
    # (track01.bin, track02.bin, …) from standalone multi-file games — skip them.
    _DISC_IMAGE_EXTS: frozenset[str] = frozenset({".chd", ".cdi", ".iso", ".img"})
    flat_disc_titles: dict[str, set[str]] = {}  # system → set[title]
    for row in rows:
        sys = str(row["system"])
        if sys not in subfolder_exclude:
            continue
        if len(Path(str(row["relative_path"])).parts) != 2:
            continue  # only flat files at system root
        if Path(str(row["filename"])).suffix.lower() in _DISC_IMAGE_EXTS:
            flat_disc_titles.setdefault(sys, set()).add(str(row["title"]))

    # Group by (system, title, disc) — same key the exporter uses.
    # Files excluded from dedup:
    #
    # 1. Companion/cuesheet files (.cue, .gdi, etc.) — they describe a primary
    #    disc image and must travel with it, never be recycled independently.
    #
    # 2. Files inside subfolders of folder_based systems (relative_path depth ≥ 3)
    #    that do NOT have a matching flat disc image at the system root.
    #    - scummvm/dos/windows: all subfolder files excluded (no CHD supersedes them)
    #    - dreamcast/megacd: track*.bin excluded; a named .cdi whose title matches
    #      a flat CHD is included so the CHD wins the dedup contest
    #    - switch: depth-3 files (base game) included; depth-4+ (updates) excluded
    groups: dict[tuple[str, str, str | None], list] = {}
    for row in rows:
        if Path(str(row["filename"])).suffix.lower() in _COMPANION_EXTENSIONS:
            continue
        sys = str(row["system"])
        parts = Path(str(row["relative_path"])).parts
        if sys in folder_based and len(parts) >= 3:
            if sys in subfolder_exclude:
                # Allow through only if a flat disc image exists with the same title
                if str(row["title"]) not in flat_disc_titles.get(sys, set()):
                    continue  # generic data file (track01.bin etc.) — skip
            else:
                # folder_based but not subfolder_exclude (e.g. switch):
                # depth-4+ are supplementary packages (updates/), skip them
                if len(parts) >= 4:
                    continue
        # Use lowercase title for grouping so that naming-convention differences
        # ("The" vs "the", "SkyHawk" vs "Skyhawk") don't create phantom groups.
        # The kept/displayed title still comes from keeper["title"] (original case).
        #
        # Synthetic disc: old No-Intro style "(NA - Disc 1)" leaves disc=None in
        # the DB because the parser only sees one parenthesised token, not two.
        # If we grouped those files by (title, None) every disc of the same game
        # would collapse into one group and get incorrectly flagged as duplicates.
        # Fix: when disc=None, check the filename for an embedded disc number and
        # promote it to a synthetic disc key so each disc remains its own group.
        disc_val = row["disc"]
        if disc_val is None:
            m = _EMBEDDED_DISC_RE.search(str(row["filename"]))
            if m:
                disc_val = f"(Disc {m.group(1)})"  # synthetic — prevents cross-disc grouping
        key = (sys, str(row["title"]).lower(), disc_val)
        groups.setdefault(key, []).append(row)

    items: list[DedupPlanItem] = []
    dup_groups = 0

    for (_sys, _title, _disc), group_rows in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1], x[0][2] or "")):
        if len(group_rows) <= 1:
            continue
        dup_groups += 1

        sorted_rows = sorted(
            group_rows,
            key=lambda r: (
                region_rank.get(str(r["region"]) if r["region"] else "", len(region_rank)),
                int(r["is_beta"]),
                int(r["is_proto"]),
                int(r["is_hack"]),
                _format_rank(str(r["filename"])),
                str(r["filename"]),
            ),
        )

        keeper = sorted_rows[0]
        for loser in sorted_rows[1:]:
            items.append(DedupPlanItem(
                loser_path=roms_root / str(loser["relative_path"]),
                keeper_path=roms_root / str(keeper["relative_path"]),
                loser_rel=str(loser["relative_path"]),
                keeper_rel=str(keeper["relative_path"]),
                system=str(keeper["system"]),
                title=str(keeper["title"]),
                reason=_describe_reason(keeper, loser, region_rank),
            ))

    return items, {"total_roms": len(rows), "groups": dup_groups}


def _describe_reason(keeper, loser, region_rank: dict[str, int]) -> str:
    k_region = str(keeper["region"]) if keeper["region"] else "(none)"
    l_region = str(loser["region"]) if loser["region"] else "(none)"
    if k_region != l_region:
        return f"region: {k_region} beats {l_region}"
    if int(keeper["is_beta"]) < int(loser["is_beta"]):
        return "not-beta beats beta"
    if int(keeper["is_proto"]) < int(loser["is_proto"]):
        return "not-proto beats proto"
    if int(keeper["is_hack"]) < int(loser["is_hack"]):
        return "not-hack beats hack"
    kf = _format_rank(str(keeper["filename"]))
    lf = _format_rank(str(loser["filename"]))
    if kf < lf:
        return f"format: {Path(str(keeper['filename'])).suffix} beats {Path(str(loser['filename'])).suffix}"
    return "filename sort order"


def _format_rank(filename: str) -> int:
    return _FORMAT_RANK.get(Path(filename).suffix.lower(), 99)


def _print_header(
    summary: DedupSummary,
    recycle_bin: Path,
    regions: list[str],
    console,
    romm_dupes: bool = False,
) -> None:
    total_groups = summary.duplicate_groups + summary.romm_groups
    if console and Table:
        table = Table(show_header=False, box=None)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Total ROMs in DB:", str(summary.total_roms))
        table.add_row("Duplicate groups (title):", str(summary.duplicate_groups))
        if romm_dupes:
            table.add_row("Duplicate groups (ROMM):", str(summary.romm_groups))
            if summary.romm_skipped_unclean:
                table.add_row("ROMM groups skipped (unclean):", str(summary.romm_skipped_unclean))
            if summary.romm_skipped_badmap:
                table.add_row("ROMM groups skipped (bad mapping):", str(summary.romm_skipped_badmap))
        table.add_row("Files to recycle:", str(summary.files_to_move))
        table.add_row("Preferred regions:", ", ".join(regions))
        table.add_row("Recycle bin:", str(recycle_bin / "roms"))
        table.add_row("Mode:", "EXECUTE" if not summary.dry_run else "DRY RUN")
        console.print(table)
    else:
        print(f"Total ROMs in DB:        {summary.total_roms}")
        print(f"Dup groups (title):      {summary.duplicate_groups}")
        if romm_dupes:
            print(f"Dup groups (ROMM):       {summary.romm_groups}")
            if summary.romm_skipped_unclean:
                print(f"ROMM groups skipped:     {summary.romm_skipped_unclean}  (contain hack/beta/proto)")
            if summary.romm_skipped_badmap:
                print(f"ROMM groups skipped:     {summary.romm_skipped_badmap}  (bad igdb_id mapping)")
        print(f"Files to recycle:        {summary.files_to_move}")
        print(f"Preferred regions:       {', '.join(regions)}")
        print(f"Recycle bin:             {recycle_bin / 'roms'}")
        print(f"Mode:                    {'EXECUTE' if not summary.dry_run else 'DRY RUN'}")
    print()


def _print_plan(items: list[DedupPlanItem], console) -> None:
    last_group: tuple[str, str] | None = None
    for item in items:
        group_key = (item.system, item.title)
        if group_key != last_group:
            line = f"  KEEP  [{item.system}] {Path(item.keeper_rel).name}"
            _print(console, line, style="bold")
            last_group = group_key
        _print(console, f"  MOVE  {Path(item.loser_rel).name}  ({item.reason})")
    if items:
        print()


def _print_romm_plan(items: list[DedupPlanItem], romm_stats: dict, console) -> None:
    """Print the ROMM cross-title dedup section with a clear header and warning."""
    _print(console, "── ROMM cross-title duplicates ──────────────────────────────────", style="bold cyan" if console else "")
    _print(
        console,
        "  These files share an IGDB id in ROMM but have different No-Intro titles\n"
        "  (e.g. regional market names).  Groups containing hacks/betas are excluded.\n"
        "  Review carefully — ROMM igdb_id mapping is occasionally imprecise.",
        style="dim",
    )
    if romm_stats.get("skipped_unclean"):
        _print(
            console,
            f"  ({romm_stats['skipped_unclean']} group(s) skipped — contained hack/beta/proto ROMs)",
            style="yellow",
        )
    if romm_stats.get("skipped_badmap"):
        _print(
            console,
            f"  ({romm_stats['skipped_badmap']} group(s) skipped — bad igdb_id mapping detected)",
            style="yellow",
        )
    _print(
        console,
        "  Tip: add persistent false-positive igdb_ids to dedup.romm_skip_igdb_ids in config.yaml",
        style="dim",
    )
    print()

    if not items:
        _print(console, "  No clean cross-title ROMM duplicates found.\n")
        return

    last_group: tuple[str, str] | None = None
    for item in items:
        group_key = (item.system, item.title)
        if group_key != last_group:
            _print(console, f"  KEEP  [{item.system}] {Path(item.keeper_rel).name}  — ROMM: {item.title!r}", style="bold")
            last_group = group_key
        _print(console, f"  MOVE  {Path(item.loser_rel).name}  ({item.reason})")
    print()


def _safe_move(src: Path, dst: Path) -> None:
    try:
        src.rename(dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # Cross-device (NFS / different filesystem): copy then delete
    shutil.copy2(src, dst)
    try:
        src.unlink()
    except OSError as exc:
        try:
            dst.unlink()
        except OSError:
            pass
        raise PermissionError(
            f"Copied to recycle bin but could not delete original '{src.name}'. Try running with sudo."
        ) from exc


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)
