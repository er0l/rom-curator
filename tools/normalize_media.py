"""Normalize media subfolders to Batocera's two-folder convention.

Many systems end up with media from multiple scrapers in parallel trees:

  Batocera / Skyscraper style  →  images/ and videos/
    images/{title}-image.png       (box art)
    images/{title}-thumb.png       (screenshot)
    images/{title}-marquee.png     (marquee / wheel / logo)
    videos/{title}-video.mp4

  ES-DE / RetroArch / plain-stem style  →  individual subfolders
    wheel/{stem}.png               (wheel / logo art)
    boxart/{stem}.png              (box front)
    snap/{stem}.mp4                (video snap)
    cartart/{stem}.png             (cartridge art)
    marquee/{stem}.png
    fanarts/{stem}.png
    flyer/{stem}.png
    screenshots/{stem}.png
    logos/{stem}.png

This command converts the plain-stem subfolders into the Batocera convention:
  1. Looks up each file's stem (case-insensitive, trailing region tags stripped)
     to find its ROM title in the database.
  2. Constructs the destination:  images/{title}-{suffix}.ext
                               or  videos/{title}-video.ext
  3. If the destination already exists the source file is considered
     "superseded" — reported but left in place (or moved to recycle bin
     with --clean-superseded).
  4. Otherwise the file is moved and renamed (--execute to apply).

Recommended workflow
--------------------
  python3 romcurator.py rename-media        # fix old-tag names first
  python3 romcurator.py normalize-media     # preview consolidation
  python3 romcurator.py normalize-media --execute [--clean-superseded]
  python3 romcurator.py clean-media         # remove remaining orphans
"""

from __future__ import annotations

import errno
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core.database import InventoryDatabase
from tools.clean_media import _IGNORED_FILENAMES, _IGNORED_PREFIXES
from tools.rename_media import _strip_trailing_tags

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


# ------------------------------------------------------------------
# Subfolder → (destination_folder, suffix) mapping
# ------------------------------------------------------------------
# Each entry describes how to move a plain-stem file into the
# Batocera two-folder layout.  suffix is appended between the title
# and the extension, e.g. "-marquee" → "Super Mario World-marquee.png".
#
# Folders that already use the suffix convention (images/, videos/)
# are intentionally absent — they need no conversion.

FOLDER_MAP: dict[str, tuple[str, str]] = {
    "wheel":       ("images", "-marquee"),
    "marquee":     ("images", "-marquee"),
    "logos":       ("images", "-marquee"),
    "boxart":      ("images", "-image"),
    "mixart":      ("images", "-image"),
    "snap":        ("videos", "-video"),
    "cartart":     ("images", "-cartart"),
    "fanarts":     ("images", "-fanart"),
    "flyer":       ("images", "-fanart"),
    "screenshots": ("images", "-thumb"),
    "backcovers":  ("images", "-backcover"),
}

# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

@dataclass
class MoveProposal:
    src: Path
    dst: Path
    rel_src: str   # relative to roms_root, for display
    rel_dst: str


@dataclass
class SupersededFile:
    src: Path
    dst: Path      # the existing destination that blocks the move
    rel_src: str
    rel_dst: str


@dataclass
class NormalizeMediaSummary:
    total_files: int = 0
    already_in_place: int = 0   # already in images/ or videos/ with suffix style
    proposals: int = 0
    superseded: int = 0         # dest already exists — file is redundant
    no_match: int = 0           # no ROM found in DB — left for clean-media
    moved: int = 0
    cleaned_superseded: int = 0
    errors: int = 0
    dry_run: bool = True


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def run_normalize_media(
    config: dict[str, object],
    *,
    systems: list[str] | None = None,
    source_folders: list[str] | None = None,
    clean_superseded: bool = False,
    execute: bool = False,
) -> NormalizeMediaSummary:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root     = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()
    recycle_bin   = Path(str(paths.get("recycle_bin", "/mnt/storage/recycle_bin"))).expanduser()

    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    folders_to_process: dict[str, tuple[str, str]]
    if source_folders:
        folders_to_process = {f: FOLDER_MAP[f] for f in source_folders if f in FOLDER_MAP}
        unknown = [f for f in source_folders if f not in FOLDER_MAP]
        if unknown:
            raise ValueError(f"Unknown source folders: {', '.join(unknown)}. "
                             f"Known: {', '.join(FOLDER_MAP)}")
    else:
        folders_to_process = FOLDER_MAP

    console = Console() if Console else None
    summary = NormalizeMediaSummary(dry_run=not execute)

    scan_systems: list[str]
    if systems:
        scan_systems = systems
    else:
        scan_systems = sorted(d.name for d in roms_root.iterdir() if d.is_dir())

    all_proposals:  list[MoveProposal]  = []
    all_superseded: list[SupersededFile] = []

    with InventoryDatabase(database_path) as db:
        db.initialize()

        for system in scan_systems:
            system_dir = roms_root / system
            if not system_dir.is_dir():
                _print(console, f"Warning: system folder not found: {system_dir}", style="yellow")
                continue

            rows = db.fetch_all(
                "SELECT DISTINCT filename, title FROM roms WHERE system = ?",
                (system,),
            )
            # case-insensitive stem → canonical title
            stem_to_title: dict[str, str] = {
                Path(str(r["filename"])).stem.lower(): str(r["title"])
                for r in rows
            }
            # For files with scraper-suffix style, base = stem with suffix stripped
            title_lower_to_title: dict[str, str] = {
                str(r["title"]).lower(): str(r["title"]) for r in rows
            }

            for src_folder, (dst_folder, suffix) in folders_to_process.items():
                src_dir = system_dir / src_folder
                dst_dir = system_dir / dst_folder
                if not src_dir.is_dir():
                    continue

                for src_file in sorted(src_dir.iterdir()):
                    if not src_file.is_file():
                        continue
                    if (src_file.name in _IGNORED_FILENAMES
                            or src_file.name.startswith(_IGNORED_PREFIXES)):
                        continue

                    summary.total_files += 1
                    stem = src_file.stem
                    ext  = src_file.suffix

                    # ---- Resolve title from DB (case-insensitive + tag-stripping) ----
                    title = _resolve_title(stem, stem_to_title, title_lower_to_title)
                    if title is None:
                        summary.no_match += 1
                        continue  # leave for clean-media

                    # ---- Build destination path ----
                    new_name = title + suffix + ext
                    dst_file = dst_dir / new_name

                    if dst_file == src_file:
                        # File is already exactly where it should be (shouldn't happen
                        # since we exclude images/ and videos/ from folders_to_process).
                        summary.already_in_place += 1
                        continue

                    rel_src = str(src_file.relative_to(roms_root))
                    rel_dst = str(dst_file.relative_to(roms_root))

                    if dst_file.exists():
                        # A file already exists at the destination (typically the
                        # Batocera-scraped version).  The source is superseded.
                        summary.superseded += 1
                        all_superseded.append(SupersededFile(
                            src=src_file, dst=dst_file, rel_src=rel_src, rel_dst=rel_dst,
                        ))
                    else:
                        summary.proposals += 1
                        all_proposals.append(MoveProposal(
                            src=src_file, dst=dst_file, rel_src=rel_src, rel_dst=rel_dst,
                        ))

    _print_header(summary, folders_to_process, systems, clean_superseded, console)
    _print_proposals(all_proposals, console)
    if all_superseded:
        _print_superseded(all_superseded, clean_superseded, console)

    if not execute:
        _print(
            console,
            f"\nDRY RUN — {summary.proposals} move(s) proposed, "
            f"{summary.superseded} superseded (dest exists), "
            f"{summary.no_match} unmatched (no ROM in DB). "
            "Pass --execute to apply.",
            style="bold",
        )
        return summary

    # Ensure destination dirs exist.
    executed_systems: set[str] = set()
    for p in all_proposals:
        executed_systems.add(str(p.dst.parent))
    for d in executed_systems:
        Path(d).mkdir(parents=True, exist_ok=True)

    for p in all_proposals:
        try:
            p.src.rename(p.dst)
            summary.moved += 1
            _print(console, f"  MOVED  {p.rel_src}  →  {p.rel_dst}", style="green")
        except Exception as exc:
            summary.errors += 1
            _print(console, f"  ERROR  {p.rel_src}: {exc}", style="red")

    if clean_superseded:
        for s in all_superseded:
            rel = s.rel_src
            dest = recycle_bin / "roms" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _safe_move(s.src, dest)
                summary.cleaned_superseded += 1
                _print(console, f"  RECYCLE  {rel}  (superseded by {s.dst.name})", style="yellow")
            except Exception as exc:
                summary.errors += 1
                _print(console, f"  ERROR  {rel}: {exc}", style="red")

    style = "red" if summary.errors else "green"
    _print(
        console,
        f"\nDone — moved: {summary.moved}  "
        f"recycled superseded: {summary.cleaned_superseded}  "
        f"errors: {summary.errors}",
        style=style,
    )
    _print(
        console,
        "Tip: run 'clean-media' next to remove any remaining unmatched files.",
        style="yellow",
    )
    return summary


# ------------------------------------------------------------------
# Title resolution
# ------------------------------------------------------------------

def _resolve_title(
    stem: str,
    stem_to_title: dict[str, str],
    title_lower_to_title: dict[str, str],
) -> str | None:
    """Return the canonical ROM title for *stem*, or None if not found.

    Tries, in order:
      1. Exact stem match (case-insensitive).
      2. After stripping trailing No-Intro / GoodTools tags.
    """
    stem_l = stem.lower()

    # 1. Direct stem match (e.g. "Super Mario World (USA)" → title "Super Mario World")
    if stem_l in stem_to_title:
        return stem_to_title[stem_l]

    # 2. Strip trailing tags and try the title table
    stripped = _strip_trailing_tags(stem).lower()
    if stripped and stripped != stem_l and stripped in title_lower_to_title:
        return title_lower_to_title[stripped]

    return None


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

def _print_header(
    summary: NormalizeMediaSummary,
    folders: dict[str, tuple[str, str]],
    systems: list[str] | None,
    clean_superseded: bool,
    console,
) -> None:
    scope = ", ".join(systems) if systems else "all systems"
    mode  = "EXECUTE" if not summary.dry_run else "DRY RUN"
    folder_list = ", ".join(
        f"{src}/ → {dst}/{suf}" for src, (dst, suf) in folders.items()
    )
    if console and Table:
        table = Table(show_header=False, box=None)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Scope:",           scope)
        table.add_row("Conversions:",     folder_list)
        table.add_row("Total files:",     str(summary.total_files))
        table.add_row("Move proposals:",  str(summary.proposals))
        table.add_row("Superseded:",      str(summary.superseded)
                      + ("  (will be recycled)" if clean_superseded else "  (kept in place)"))
        table.add_row("No ROM match:",    str(summary.no_match) + "  (leave for clean-media)")
        table.add_row("Mode:",            mode)
        console.print(table)
    else:
        print(f"Scope:          {scope}")
        print(f"Total files:    {summary.total_files}")
        print(f"Move proposals: {summary.proposals}")
        print(f"Superseded:     {summary.superseded}"
              + ("  (will be recycled)" if clean_superseded else "  (kept in place)"))
        print(f"No ROM match:   {summary.no_match}  (leave for clean-media)")
        print(f"Mode:           {mode}")
    print()


def _print_proposals(proposals: list[MoveProposal], console) -> None:
    if not proposals:
        _print(console, "  Nothing to move.")
        return
    last_system = ""
    for p in proposals:
        parts = Path(p.rel_src).parts
        system = parts[0] if len(parts) > 1 else ""
        if system != last_system:
            _print(console, f"\n  [{system}]", style="bold")
            last_system = system
        _print(console, f"    {'/'.join(parts[1:])}  →  {'/'.join(Path(p.rel_dst).parts[1:])}")
    print()


def _print_superseded(
    superseded: list[SupersededFile],
    will_clean: bool,
    console,
) -> None:
    header = (
        f"  {len(superseded)} file(s) are superseded "
        f"(destination already exists in images/ or videos/):"
    )
    action = "  Will be moved to recycle bin." if will_clean else \
             "  Left in place — run with --clean-superseded to recycle them."
    _print(console, header, style="yellow")
    _print(console, action, style="yellow")
    last_system = ""
    for s in superseded[:20]:   # show first 20 to avoid wall of text
        parts = Path(s.rel_src).parts
        system = parts[0] if len(parts) > 1 else ""
        if system != last_system:
            _print(console, f"\n  [{system}]", style="dim")
            last_system = system
        _print(console, f"    {'/'.join(parts[1:])}  ↔  {Path(s.rel_dst).name}", style="dim")
    if len(superseded) > 20:
        _print(console, f"\n  … and {len(superseded) - 20} more.", style="dim")
    print()


def _safe_move(src: Path, dst: Path) -> None:
    try:
        src.rename(dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copy2(src, dst)
    try:
        src.unlink()
    except OSError as exc:
        try:
            dst.unlink()
        except OSError:
            pass
        raise PermissionError(
            f"Copied to recycle bin but could not delete original '{src.name}'."
        ) from exc


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)
