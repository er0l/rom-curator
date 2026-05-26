"""Rename orphaned media files whose names contain old ROM-set tags.

Many scrapers (Skyscraper, etc.) were run against older No-Intro / GoodTools
ROM sets that used region and revision codes inside the filename — e.g.:

    Aladdin (E) [!]-image.png       ← old set: "Aladdin (E) [!].zip"
    Alien 3 (USA, Europe) (Rev A)-image.png
    Ariel - The Little Mermaid (USA, Europe)-image.png

Modern No-Intro sets rename those ROMs; the titles are stripped of redundant
qualifiers.  ``clean-media`` then flags the media as orphaned because the
exact title no longer matches.

This tool:

1. Scans the same media subfolders as ``clean-media``.
2. For each file that does NOT already match a ROM, strips trailing
   parenthetical / bracket groups from the name and tries again.
3. If the stripped name uniquely matches exactly one ROM title, the file
   is a rename candidate.
4. In dry-run mode (default) it prints a proposed rename table.
5. With ``--execute`` it actually renames (or moves) the files.

Workflow
--------
Run ``rename-media`` first, then ``clean-media`` to remove any remaining
unmatched files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.database import InventoryDatabase

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


# ------------------------------------------------------------------
# Re-use defaults from clean_media to keep behaviour consistent.
# ------------------------------------------------------------------
from tools.clean_media import (
    DEFAULT_MEDIA_FOLDERS,
    _IGNORED_FILENAMES,
    _IGNORED_PREFIXES,
    _SCRAPER_SUFFIXES,
    _strip_scraper_suffix,
)


# ------------------------------------------------------------------
# Tag-stripping regex
# ------------------------------------------------------------------

# Strips one or more trailing  (...)  or  [...]  groups, plus any leading
# whitespace before each group.  These are No-Intro / GoodTools qualifiers
# that appear at the end of a filename stem, e.g.:
#   "Alien 3 (USA, Europe) (Rev A)"  →  "Alien 3"
#   "Aladdin (E) [!]"               →  "Aladdin"
#   "Road Rash 3 (U) [!]"           →  "Road Rash 3"
# Parentheticals that are PART of the title (not a suffix) are not affected
# because this regex only strips from the end.
_TRAILING_TAGS_RE = re.compile(r"(\s*[\(\[][^\(\)\[\]]*[\)\]])+$")


def _strip_trailing_tags(s: str) -> str:
    """Strip trailing No-Intro / GoodTools tags, returning the bare title."""
    return _TRAILING_TAGS_RE.sub("", s).strip()


# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

@dataclass
class RenameProposal:
    src: Path           # absolute path of the current file
    dst: Path           # absolute path of the proposed new name
    rel_src: str        # path relative to roms_root (for display)
    rel_dst: str        # new relative path (for display)
    matched_title: str  # ROM title it was matched to


@dataclass
class RenameMediaSummary:
    total_files: int = 0
    already_matched: int = 0
    proposals: int = 0
    renamed: int = 0
    skipped_ambiguous: int = 0
    skipped_conflict: int = 0
    errors: int = 0
    dry_run: bool = True
    conflicts: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def run_rename_media(
    config: dict[str, object],
    *,
    systems: list[str] | None = None,
    media_folders: list[str] | None = None,
    execute: bool = False,
    mappings: dict[str, object] | None = None,
) -> RenameMediaSummary:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root     = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()

    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    media_config = config.get("media", {})
    if not isinstance(media_config, dict):
        media_config = {}
    folders_to_check: list[str] = (
        media_folders
        or media_config.get("folders")  # type: ignore[assignment]
        or DEFAULT_MEDIA_FOLDERS
    )

    console = Console() if Console else None
    summary = RenameMediaSummary(dry_run=not execute)

    scan_systems: list[str]
    if systems:
        scan_systems = systems
    else:
        scan_systems = sorted(d.name for d in roms_root.iterdir() if d.is_dir())

    all_proposals: list[RenameProposal] = []

    with InventoryDatabase(database_path) as db:
        db.initialize()

        for system in scan_systems:
            # Resolve the NAS folder via mappings (handles subpath systems such
            # as mame2003-plus whose nas is 'arcade/mame2003-plus').  For subpath
            # systems the media lives in the parent folder (e.g. arcade/).
            sys_meta = (mappings or {}).get(system, {})
            nas_folder = (
                str(sys_meta.get("nas"))
                if isinstance(sys_meta, dict) and sys_meta.get("nas")
                else system
            )
            if "/" in nas_folder:
                parent_nas = nas_folder.rsplit("/", 1)[0]
                system_dir = roms_root / parent_nas
            else:
                system_dir = roms_root / nas_folder

            if not system_dir.is_dir():
                _print(console, f"Warning: system folder not found: {system_dir}", style="yellow")
                continue

            # Build lookup structures — all lowercased for case-insensitive ops.
            # Include co-resident systems (e.g. arcade when processing
            # mame2003-plus) so their media is not incorrectly treated as
            # unmatched when both share the same media folder.
            from tools.clean_media import _co_resident_systems
            db_systems = _co_resident_systems(system, nas_folder, mappings or {})
            placeholders = ",".join("?" * len(db_systems))
            rows = db.fetch_all(
                f"SELECT DISTINCT filename, title FROM roms WHERE system IN ({placeholders})",
                db_systems,
            )
            # Case-insensitive stem / title sets (for "already matched" check).
            rom_stems_lower:  set[str] = {Path(str(r["filename"])).stem.lower() for r in rows}
            rom_titles_lower: set[str] = {str(r["title"]).lower() for r in rows}

            # title_lower → canonical title string (for reconstructing filenames).
            title_canonical: dict[str, str] = {str(r["title"]).lower(): str(r["title"]) for r in rows}

            for folder_name in folders_to_check:
                media_dir = system_dir / folder_name
                if not media_dir.is_dir():
                    continue

                for media_file in sorted(media_dir.iterdir()):
                    if not media_file.is_file():
                        continue
                    if (media_file.name in _IGNORED_FILENAMES
                            or media_file.name.startswith(_IGNORED_PREFIXES)):
                        continue

                    summary.total_files += 1
                    stem   = media_file.stem
                    base   = _strip_scraper_suffix(stem)          # strip -image, -video, …
                    suffix = stem[len(base):]                      # e.g. "-image", ""
                    ext    = media_file.suffix                     # e.g. ".png"

                    stem_l = stem.lower()
                    base_l = base.lower()

                    # ---------- Step 1: already matched? skip. ----------
                    if stem_l in rom_stems_lower or base_l in rom_stems_lower or base_l in rom_titles_lower:
                        summary.already_matched += 1
                        continue

                    # ---------- Step 2: strip trailing tags, retry. ----------
                    stripped = _strip_trailing_tags(base)
                    stripped_l = stripped.lower()

                    if not stripped or stripped_l == base_l:
                        # Nothing to strip — genuinely orphaned.
                        continue

                    if stripped_l not in rom_titles_lower:
                        # Stripping tags didn't help either.
                        continue

                    # Matched!  Build the proposed new filename.
                    canonical_title = title_canonical[stripped_l]
                    new_stem = canonical_title + suffix            # e.g. "Aladdin-image"
                    new_name = new_stem + ext                      # e.g. "Aladdin-image.png"
                    dst = media_dir / new_name

                    # Skip if the destination already exists (would overwrite).
                    if dst.exists() and dst != media_file:
                        summary.skipped_conflict += 1
                        summary.conflicts.append(
                            f"{media_file.relative_to(roms_root)}  →  {new_name}"
                        )
                        continue

                    rel_src = str(media_file.relative_to(roms_root))
                    rel_dst = str(dst.relative_to(roms_root))
                    all_proposals.append(
                        RenameProposal(
                            src=media_file,
                            dst=dst,
                            rel_src=rel_src,
                            rel_dst=rel_dst,
                            matched_title=canonical_title,
                        )
                    )
                    summary.proposals += 1

    _print_header(summary, folders_to_check, systems, console)
    _print_proposals(all_proposals, console)
    if summary.conflicts:
        _print(console, f"  Skipped {summary.skipped_conflict} conflict(s) — destination already exists:", style="yellow")
        for c in summary.conflicts:
            _print(console, f"    {c}", style="yellow")
        print()

    if not execute:
        _print(
            console,
            f"\nDRY RUN — {summary.proposals} rename(s) proposed across "
            f"{summary.total_files} media file(s) scanned. "
            "Pass --execute to apply.",
            style="bold",
        )
        return summary

    for proposal in all_proposals:
        try:
            proposal.src.rename(proposal.dst)
            summary.renamed += 1
            _print(console, f"  RENAMED  {proposal.rel_src}  →  {proposal.dst.name}", style="green")
        except Exception as exc:
            summary.errors += 1
            _print(console, f"  ERROR    {proposal.rel_src}: {exc}", style="red")

    style = "red" if summary.errors else "green"
    _print(
        console,
        f"\nDone — renamed: {summary.renamed}  "
        f"skipped (conflict): {summary.skipped_conflict}  "
        f"errors: {summary.errors}",
        style=style,
    )
    if summary.renamed:
        _print(
            console,
            "Tip: run 'clean-media' next to remove any remaining unmatched files.",
            style="yellow",
        )
    return summary


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

def _print_header(
    summary: RenameMediaSummary,
    folders: list[str],
    systems: list[str] | None,
    console,
) -> None:
    scope = ", ".join(systems) if systems else "all systems"
    mode  = "EXECUTE" if not summary.dry_run else "DRY RUN"
    if console and Table:
        table = Table(show_header=False, box=None)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Scope:",          scope)
        table.add_row("Media folders:",  ", ".join(folders))
        table.add_row("Total files:",    str(summary.total_files))
        table.add_row("Already matched:", str(summary.already_matched))
        table.add_row("Rename proposals:", str(summary.proposals))
        table.add_row("Mode:",           mode)
        console.print(table)
    else:
        print(f"Scope:            {scope}")
        print(f"Media folders:    {', '.join(folders)}")
        print(f"Total files:      {summary.total_files}")
        print(f"Already matched:  {summary.already_matched}")
        print(f"Rename proposals: {summary.proposals}")
        print(f"Mode:             {mode}")
    print()


def _print_proposals(proposals: list[RenameProposal], console) -> None:
    if not proposals:
        _print(console, "  No rename candidates found.")
        return
    last_system = ""
    for p in proposals:
        parts = Path(p.rel_src).parts
        system = parts[0] if len(parts) > 1 else ""
        if system != last_system:
            _print(console, f"\n  [{system}]", style="bold")
            last_system = system
        old_name = Path(p.rel_src).name
        new_name = Path(p.rel_dst).name
        _print(console, f"    {old_name}  →  {new_name}  (title: {p.matched_title!r})")
    print()


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)
