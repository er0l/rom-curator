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
    moved: int = 0
    errors: int = 0
    dry_run: bool = True


def run_clean_media(
    config: dict[str, object],
    *,
    systems: list[str] | None = None,
    media_folders: list[str] | None = None,
    execute: bool = False,
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
            system_dir = roms_root / system
            if not system_dir.is_dir():
                _print(console, f"Warning: system folder not found: {system_dir}", style="yellow")
                continue

            # Build lookup sets from the DB for this system.
            # rom_stems  — full filename stems, e.g. "7th Saga, The (USA)"
            # rom_titles — parsed titles,        e.g. "7th Saga, The"
            rows = db.fetch_all(
                "SELECT DISTINCT filename, title FROM roms WHERE system = ?",
                (system,),
            )
            rom_stems:  set[str] = {Path(str(r["filename"])).stem for r in rows}
            rom_titles: set[str] = {str(r["title"]) for r in rows}

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
                    stem = media_file.stem  # filename without final extension
                    base = _strip_scraper_suffix(stem)

                    # A file is NOT orphaned if:
                    #   - its full stem matches a ROM filename stem  (boxart-style)
                    #   - its suffix-stripped base matches a ROM stem (edge cases)
                    #   - its suffix-stripped base matches a ROM title (scraper-style)
                    if stem in rom_stems or base in rom_stems or base in rom_titles:
                        continue

                    rel = str(media_file.relative_to(roms_root))
                    orphaned.append((media_file, rel))
                    summary.orphaned += 1

    _print_header(summary, recycle_bin, folders_to_check, systems, console)
    _print_plan(orphaned, console)

    if not execute:
        _print(
            console,
            f"\nDRY RUN complete — {summary.orphaned} orphaned file(s) across "
            f"{summary.total_files} media file(s) scanned. "
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
        table.add_row("Recycle bin:",   str(recycle_bin / "roms"))
        table.add_row("Mode:",          "EXECUTE" if not summary.dry_run else "DRY RUN")
        console.print(table)
    else:
        print(f"Scope:          {scope}")
        print(f"Media folders:  {', '.join(folders)}")
        print(f"Total files:    {summary.total_files}")
        print(f"Orphaned:       {summary.orphaned}")
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
