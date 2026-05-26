"""Clean orphaned media files from images/, videos/, and other metadata folders.

For each system folder, scans the configured media subfolders and removes
files whose name cannot be matched to any ROM in the inventory database.

Two naming conventions are handled automatically:

  1. Full ROM stem  (boxart, snap, wheel, cartart, ...):
       "7th Saga, The (USA).png"  →  matches ROM "7th Saga, The (USA).zip"

  2. Scraper-suffix style  (images, videos, ...):
       "7th Saga, The-image.png"  →  strip "-image", match title "7th Saga, The"
       "1942-video.mp4"           →  strip "-video",  match title "1942"

A media file is kept if either its full stem OR its suffix-stripped base
matches a ROM filename stem or parsed title in the database for that system.

Files are moved to the recycle bin (preserving relative path) rather than
deleted permanently.  Run 'inventory' first to ensure the database reflects
the current state of the archive.
"""

from __future__ import annotations

import errno
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


# Subfolder names that are considered media metadata (not ROM files).
DEFAULT_MEDIA_FOLDERS: list[str] = [
    "images",
    "videos",
    "snap",
    "boxart",
    "wheel",
    "cartart",
    "mixart",
    "manuals",
    "logos",
    "fanarts",
    "backcovers",
    "screenshots",
    "marquees",
    "media",
]

# Files that should never be touched regardless of where they live.
_IGNORED_FILENAMES: frozenset[str] = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini", "gamelist.xml", "_info.txt",
})
_IGNORED_PREFIXES: tuple[str, ...] = ("._",)

# Suffixes appended by common scrapers (Skyscraper, ES-DE, Batocera scraper).
# Stripped from the media filename stem before matching against ROM titles.
_SCRAPER_SUFFIXES: frozenset[str] = frozenset({
    "-image",
    "-thumb",
    "-marquee",
    "-video",
    "-snap",
    "-boxart",
    "-wheel",
    "-logo",
    "-cartart",
    "-mixart",
    "-manual",
    "-box",
    "-disc",
    "-screenshot",
    "-fanart",
    "-backcover",
})


@dataclass
class CleanMediaSummary:
    total_files: int = 0
    orphaned: int = 0
    superseded: int = 0
    png_preferred: int = 0   # JPG/JPEG duplicates removed in favour of PNG
    moved: int = 0
    errors: int = 0
    dry_run: bool = True


# Suffix-style variants that can supersede a plain-stem file in images/.
# If any of these exists for the same title, the plain-stem file is redundant.
_IMAGE_SUPERSEDING_SUFFIXES: tuple[str, ...] = (
    "-image", "-thumb", "-marquee", "-fanart", "-screenshot",
)
_VIDEO_SUPERSEDING_SUFFIXES: tuple[str, ...] = ("-video",)

# Extensions used when probing for superseding files.
_IMAGE_EXTS_SET: tuple[str, ...] = (".png", ".jpg", ".jpeg")
_VIDEO_EXTS_SET: tuple[str, ...] = (".mp4", ".avi", ".mkv")


def _is_superseded(
    stem: str,
    title: str,
    folder_name: str,
    folder_index: dict[str, str],  # lower_filename → actual_filename (from _build_folder_index)
) -> bool:
    """Return True if a plain-stem media file is shadowed by a suffix-style version.

    A plain-stem file like ``images/drakton.png`` is superseded when a
    suffix-style file like ``images/drakton-image.png`` already exists in the
    same folder, because gen-gamelist always picks the suffix-style file first.
    Only checks files that have NO scraper suffix (i.e. pure plain-stem files).
    """
    # Only consider plain-stem files (no scraper suffix on the stem).
    if _strip_scraper_suffix(stem) != stem:
        return False

    if folder_name == "images":
        suffixes = _IMAGE_SUPERSEDING_SUFFIXES
        exts     = _IMAGE_EXTS_SET
    elif folder_name == "videos":
        suffixes = _VIDEO_SUPERSEDING_SUFFIXES
        exts     = _VIDEO_EXTS_SET
    else:
        return False  # other folders don't have a suffix convention

    title_l = title.lower()
    for suffix in suffixes:
        for ext in exts:
            if (title_l + suffix + ext) in folder_index:
                return True
    return False


def _build_folder_index(folder: Path) -> dict[str, str]:
    """Return {lower_filename: actual_filename} for all files in *folder*."""
    if not folder.is_dir():
        return {}
    return {f.name.lower(): f.name for f in folder.iterdir() if f.is_file()}


def run_clean_media(
    config: dict[str, object],
    *,
    systems: list[str] | None = None,
    media_folders: list[str] | None = None,
    remove_superseded: bool = False,
    prefer_png: bool = False,
    execute: bool = False,
    mappings: dict[str, object] | None = None,
) -> CleanMediaSummary:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root   = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()
    recycle_bin = Path(str(paths.get("recycle_bin", "/mnt/storage/recycle_bin"))).expanduser()

    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database does not exist: {database_path}")

    # Media folder names — CLI override → config.yaml → built-in defaults.
    media_config = config.get("media", {})
    if not isinstance(media_config, dict):
        media_config = {}
    folders_to_check: list[str] = (
        media_folders
        or media_config.get("folders")  # type: ignore[assignment]
        or DEFAULT_MEDIA_FOLDERS
    )

    console = Console() if Console else None
    summary = CleanMediaSummary(dry_run=not execute)

    # Resolve which systems to scan.
    if systems:
        scan_systems = systems
    else:
        scan_systems = sorted(d.name for d in roms_root.iterdir() if d.is_dir())

    orphaned: list[tuple[Path, str]] = []  # (absolute_path, relative_to_roms_root)

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

            # Build lookup sets from the DB for this system.
            # All sets are lowercased for case-insensitive matching — scrapers and
            # ROM sets often disagree on capitalisation (e.g. "Aaahh!!!" vs "AAAHH!!!").
            # rom_stems  — full filename stems, e.g. "7th saga, the (usa)"
            # rom_titles — parsed titles,        e.g. "7th saga, the"
            rows = db.fetch_all(
                "SELECT DISTINCT filename, title FROM roms WHERE system = ?",
                (system,),
            )
            rom_stems:  set[str] = {Path(str(r["filename"])).stem.lower() for r in rows}
            rom_titles: set[str] = {str(r["title"]).lower() for r in rows}
            # stem_lower → canonical title (needed for superseded check)
            stem_to_title: dict[str, str] = {
                Path(str(r["filename"])).stem.lower(): str(r["title"]) for r in rows
            }
            title_lower_to_title: dict[str, str] = {
                str(r["title"]).lower(): str(r["title"]) for r in rows
            }

            for folder_name in folders_to_check:
                media_dir = system_dir / folder_name
                if not media_dir.is_dir():
                    continue

                # Build a case-insensitive index of this folder for superseded
                # checks and PNG-preference deduplication.
                folder_index: dict[str, str] = (
                    _build_folder_index(media_dir)
                    if (remove_superseded or prefer_png)
                    else {}
                )

                # PNG-preference pass: collect stems that have both a PNG and a
                # JPG/JPEG so the per-file loop below can flag the lossy copy.
                png_preferred_stems: set[str] = set()
                if prefer_png and folder_index:
                    png_preferred_stems = _find_jpg_duplicates(folder_index)

                for media_file in sorted(media_dir.iterdir()):
                    if not media_file.is_file():
                        continue
                    if (media_file.name in _IGNORED_FILENAMES
                            or media_file.name.startswith(_IGNORED_PREFIXES)):
                        continue

                    summary.total_files += 1
                    stem = media_file.stem  # filename without final extension
                    base = _strip_scraper_suffix(stem)

                    # A file is NOT orphaned if (all comparisons case-insensitive):
                    #   - its full stem matches a ROM filename stem  (boxart-style)
                    #   - its suffix-stripped base matches a ROM stem (edge cases)
                    #   - its suffix-stripped base matches a ROM title (scraper-style)
                    stem_l = stem.lower()
                    base_l = base.lower()
                    matched = (
                        stem_l in rom_stems or base_l in rom_stems or base_l in rom_titles
                    )

                    if not matched:
                        rel = str(media_file.relative_to(roms_root))
                        orphaned.append((media_file, rel))
                        summary.orphaned += 1
                        continue

                    # Matched a ROM — check if it's superseded by a suffix-style version.
                    if remove_superseded and folder_name in ("images", "videos"):
                        # Resolve the canonical title for this file.
                        title = (
                            stem_to_title.get(stem_l)
                            or stem_to_title.get(base_l)
                            or title_lower_to_title.get(base_l)
                        )
                        if title and _is_superseded(stem, title, folder_name, folder_index):
                            rel = str(media_file.relative_to(roms_root))
                            orphaned.append((media_file, rel))
                            summary.superseded += 1
                            continue

                    # PNG-preference: flag JPG/JPEG when a PNG of the same stem exists.
                    if prefer_png and media_file.suffix.lower() in (".jpg", ".jpeg"):
                        if stem_l in png_preferred_stems:
                            rel = str(media_file.relative_to(roms_root))
                            orphaned.append((media_file, rel))
                            summary.png_preferred += 1

    _print_header(summary, recycle_bin, folders_to_check, systems, console)
    _print_plan(orphaned, console)

    if not execute:
        total_to_remove = summary.orphaned + summary.superseded + summary.png_preferred
        parts = [f"{summary.orphaned} orphaned"]
        if summary.superseded:
            parts.append(f"{summary.superseded} superseded")
        if summary.png_preferred:
            parts.append(f"{summary.png_preferred} jpg→png preferred")
        _print(
            console,
            f"\nDRY RUN complete — {total_to_remove} file(s) to remove "
            f"({', '.join(parts)}) across {summary.total_files} media file(s) scanned. "
            "Pass --execute to move them to the recycle bin.",
            style="bold",
        )
        return summary

    for path, rel in orphaned:
        dest = recycle_bin / "roms" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _safe_move(path, dest)
            summary.moved += 1
            _print(console, f"  MOVED  {rel}", style="green")
        except Exception as exc:
            summary.errors += 1
            _print(console, f"  ERROR  {rel}: {exc}", style="red")

    style = "red" if summary.errors else "green"
    _print(console, f"\nDone — moved: {summary.moved}  errors: {summary.errors}", style=style)
    if summary.errors:
        _print(console, "Hint: re-run with sudo if you see permission errors on NFS mounts.")
    _print(
        console,
        "Run 'inventory' to update the database if any ROMs were also changed.",
        style="yellow",
    )
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_jpg_duplicates(folder_index: dict[str, str]) -> set[str]:
    """Return the set of lowercased stems that have both a PNG and a JPG/JPEG.

    Only these stems' JPG/JPEG files should be flagged for removal — any stem
    that has *only* a JPG (no PNG counterpart) is kept as-is.
    """
    png_stems: set[str] = set()
    jpg_stems: set[str] = set()
    for lower_name in folder_index:
        p = Path(lower_name)
        ext = p.suffix
        stem = p.stem
        if ext == ".png":
            png_stems.add(stem)
        elif ext in (".jpg", ".jpeg"):
            jpg_stems.add(stem)
    return png_stems & jpg_stems   # stems that have BOTH — the JPG is redundant


def _strip_scraper_suffix(stem: str) -> str:
    """Return the core game name by stripping known scraper suffixes."""
    for suffix in _SCRAPER_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _print_header(
    summary: CleanMediaSummary,
    recycle_bin: Path,
    folders: list[str],
    systems: list[str] | None,
    console,
) -> None:
    scope = ", ".join(systems) if systems else "all systems"
    if console and Table:
        table = Table(show_header=False, box=None)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Scope:",         scope)
        table.add_row("Media folders:", ", ".join(folders))
        table.add_row("Total files:",   str(summary.total_files))
        table.add_row("Orphaned:",      str(summary.orphaned))
        if summary.superseded:
            table.add_row("Superseded:",    str(summary.superseded)
                          + "  (plain-stem shadowed by suffix-style version)")
        if summary.png_preferred:
            table.add_row("PNG preferred:", str(summary.png_preferred)
                          + "  (JPG removed — PNG counterpart exists)")
        table.add_row("Recycle bin:",   str(recycle_bin / "roms"))
        table.add_row("Mode:",          "EXECUTE" if not summary.dry_run else "DRY RUN")
        console.print(table)
    else:
        print(f"Scope:          {scope}")
        print(f"Media folders:  {', '.join(folders)}")
        print(f"Total files:    {summary.total_files}")
        print(f"Orphaned:       {summary.orphaned}")
        if summary.superseded:
            print(f"Superseded:     {summary.superseded}"
                  "  (plain-stem shadowed by suffix-style version)")
        if summary.png_preferred:
            print(f"PNG preferred:  {summary.png_preferred}"
                  "  (JPG removed — PNG counterpart exists)")
        print(f"Recycle bin:    {recycle_bin / 'roms'}")
        print(f"Mode:           {'EXECUTE' if not summary.dry_run else 'DRY RUN'}")
    print()


def _print_plan(orphaned: list[tuple[Path, str]], console) -> None:
    if not orphaned:
        _print(console, "  No orphaned media files found.")
        return
    last_system = ""
    for _, rel in orphaned:
        parts = Path(rel).parts
        system = parts[0] if len(parts) > 1 else ""
        if system != last_system:
            _print(console, f"\n  [{system}]", style="bold")
            last_system = system
        _print(console, f"    {'/'.join(parts[1:])}")
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
            f"Copied to recycle bin but could not delete original '{src.name}'. "
            "Try running with sudo."
        ) from exc


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)
