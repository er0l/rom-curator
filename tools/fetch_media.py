"""Download missing scraped media from ROMM to NAS system folders.

For each ROM that has media URLs stored in the inventory database (populated
by romm-sync), checks whether the corresponding file already exists on disk
and downloads it only if absent.

Downloaded files are placed in the full-stem subfolder layout used by
gen-gamelist:

  boxart/{stem}.jpg        ← ROM cover image
  screenshots/{stem}.jpg   ← First screenshot (index 0 only)

Existing files are never overwritten. A small delay between requests avoids
flooding the local ROMM HTTP server.

Default behaviour is a dry run — pass --execute to actually write files.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.database import InventoryDatabase

try:
    import httpx
except ImportError as exc:
    raise RuntimeError(
        "httpx is required for fetch-media. Run: pip install -r curator/requirements.txt"
    ) from exc

try:
    from dotenv import load_dotenv
except ImportError as exc:
    raise RuntimeError(
        "python-dotenv is required for fetch-media. Run: pip install -r curator/requirements.txt"
    ) from exc

try:
    from rich.console import Console
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table
except ImportError:
    Console = None
    Progress = None
    Table = None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class FetchMediaStats:
    system: str
    folder: str
    # Covers
    cover_available: int = 0    # ROMs with a cover URL in DB
    cover_present: int = 0      # already on disk
    cover_missing: int = 0      # absent — would be / were downloaded
    cover_fetched: int = 0      # actually downloaded (execute mode)
    cover_errors: int = 0
    # Screenshots
    shot_available: int = 0
    shot_present: int = 0
    shot_missing: int = 0
    shot_fetched: int = 0
    shot_errors: int = 0


# ---------------------------------------------------------------------------
# Core fetcher
# ---------------------------------------------------------------------------

def fetch_media(
    system: str,
    roms_root: Path,
    database_path: Path,
    romm_url: str,
    *,
    nas_folder: str | None = None,
    delay: float = 0.05,
    execute: bool = False,
    console=None,
) -> FetchMediaStats:
    """Analyse (and optionally download) missing cover/screenshot images for *system*."""
    folder_name = nas_folder or system
    system_dir = roms_root / folder_name
    if not system_dir.is_dir():
        raise FileNotFoundError(f"System folder not found: {system_dir}")

    stats = FetchMediaStats(system=system, folder=folder_name)

    with InventoryDatabase(database_path) as db:
        db.initialize()
        rows = db.fetch_all(
            """
            SELECT fs_stem, url_cover, url_screenshots
            FROM romm_roms
            WHERE canonical_system = ?
              AND (url_cover IS NOT NULL OR url_screenshots IS NOT NULL)
            ORDER BY fs_stem
            """,
            (system,),
        )

    if not rows:
        return stats

    romm_base = romm_url.rstrip("/")
    pending_covers: list[tuple[str, Path]] = []      # (url, dest)
    pending_shots:  list[tuple[str, Path]] = []

    for row in rows:
        stem = str(row["fs_stem"] or "")
        if not stem:
            continue

        # Cover
        cover_url = str(row["url_cover"] or "")
        if cover_url:
            stats.cover_available += 1
            dest = system_dir / "boxart" / f"{stem}.jpg"
            if dest.exists():
                stats.cover_present += 1
            else:
                stats.cover_missing += 1
                pending_covers.append((_full_url(romm_base, cover_url), dest))

        # Screenshot (first only)
        shots_raw = str(row["url_screenshots"] or "")
        shot_url = shots_raw.split(";")[0].strip() if shots_raw else ""
        if shot_url:
            stats.shot_available += 1
            dest = system_dir / "screenshots" / f"{stem}.jpg"
            if dest.exists():
                stats.shot_present += 1
            else:
                stats.shot_missing += 1
                pending_shots.append((_full_url(romm_base, shot_url), dest))

    if not execute:
        return stats

    # Execute: download pending files
    total_pending = len(pending_covers) + len(pending_shots)
    if console:
        console.print(f"\n[bold]{system}[/bold] — downloading {total_pending} file(s)…")
    else:
        print(f"\n{system} — downloading {total_pending} file(s)…")

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for url, dest in pending_covers:
            ok = _download(client, url, dest, stats, "cover")
            _print_download(console, "cover", dest.name, ok)
            if delay > 0:
                time.sleep(delay)
        for url, dest in pending_shots:
            ok = _download(client, url, dest, stats, "shot")
            _print_download(console, "shot", dest.name, ok)
            if delay > 0:
                time.sleep(delay)

    return stats


def _full_url(base: str, url: str) -> str:
    return url if url.startswith("http") else f"{base}{url}"


def _download(
    client: httpx.Client,
    url: str,
    dest: Path,
    stats: FetchMediaStats,
    kind: str,
) -> bool:
    try:
        r = client.get(url)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        if kind == "cover":
            stats.cover_fetched += 1
        else:
            stats.shot_fetched += 1
        return True
    except Exception as exc:
        if kind == "cover":
            stats.cover_errors += 1
        else:
            stats.shot_errors += 1
        return False


def _print_download(console, kind: str, filename: str, ok: bool) -> None:
    label = "cover" if kind == "cover" else "shot "
    if ok:
        msg = f"  OK     [{label}]  {filename}"
        if console:
            console.print(msg, style="green")
        else:
            print(msg)
    else:
        msg = f"  ERROR  [{label}]  {filename}"
        if console:
            console.print(msg, style="red")
        else:
            print(msg)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_fetch_media(
    config: dict[str, object],
    systems: list[str],
    mappings: dict[str, dict[str, object]],
    *,
    execute: bool = False,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    _load_env(config)

    romm_config = config.get("romm") or {}
    if not isinstance(romm_config, dict):
        romm_config = {}

    romm_url = str(os.environ.get("ROMM_URL") or romm_config.get("url") or "").rstrip("/")
    if not romm_url:
        raise ValueError("ROMM URL not configured. Add ROMM_URL=https://your-romm-instance to .env")

    roms_root     = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()
    delay         = float(romm_config.get("media_delay", 0.05))

    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database not found: {database_path}")

    console = Console() if Console else None

    all_stats: list[FetchMediaStats] = []
    for system in systems:
        nas_folder = None
        sys_meta = mappings.get(system, {})
        if isinstance(sys_meta, dict) and sys_meta.get("nas"):
            nas_folder = str(sys_meta["nas"])

        folder_name = nas_folder or system
        system_dir  = roms_root / folder_name
        if not system_dir.is_dir():
            if console:
                console.print(f"[yellow]Warning:[/yellow] folder not found: {system_dir}")
            else:
                print(f"Warning: folder not found: {system_dir}")
            continue

        try:
            stats = fetch_media(
                system, roms_root, database_path, romm_url,
                nas_folder=folder_name, delay=delay, execute=execute, console=console,
            )
            all_stats.append(stats)
        except Exception as exc:
            if console:
                console.print(f"[red]ERROR[/red] {system}: {exc}")
            else:
                print(f"ERROR {system}: {exc}")

    _print_report(all_stats, execute=execute, console=console)


def _print_report(
    all_stats: list[FetchMediaStats],
    *,
    execute: bool,
    console,
) -> None:
    if not all_stats:
        return

    mode = "EXECUTE" if execute else "DRY RUN"
    title = f"fetch-media — {mode}"

    # Columns: System | Folder | Cover: have/total | Shot: have/total | To fetch | Status
    columns = ("System", "Folder", "Covers on disk", "Covers to fetch", "Shots on disk", "Shots to fetch", "Errors")

    rows_out = []
    for s in all_stats:
        errors = s.cover_errors + s.shot_errors
        if execute:
            cover_col = f"{s.cover_present + s.cover_fetched}/{s.cover_available}"
            shot_col  = f"{s.shot_present + s.shot_fetched}/{s.shot_available}"
            cover_fetch_col = str(s.cover_fetched)
            shot_fetch_col  = str(s.shot_fetched)
        else:
            cover_col       = f"{s.cover_present}/{s.cover_available}"
            shot_col        = f"{s.shot_present}/{s.shot_available}"
            cover_fetch_col = str(s.cover_missing)
            shot_fetch_col  = str(s.shot_missing)
        rows_out.append((
            s.system, s.folder,
            cover_col, cover_fetch_col,
            shot_col,  shot_fetch_col,
            str(errors) if errors else "—",
        ))

    if console and Table:
        table = Table(title=title)
        for col in columns:
            table.add_column(col)
        for row in rows_out:
            table.add_row(*row)
        console.print(table)

        total_fetch = sum(s.cover_missing + s.shot_missing for s in all_stats)
        total_errors = sum(s.cover_errors + s.shot_errors for s in all_stats)
        if execute:
            fetched = sum(s.cover_fetched + s.shot_fetched for s in all_stats)
            console.print(f"\n[bold]EXECUTE complete[/bold] — {fetched} file(s) downloaded, {total_errors} error(s).")
        else:
            console.print(
                f"\n[bold]DRY RUN[/bold] — {total_fetch} file(s) would be downloaded. "
                "Pass [bold]--execute[/bold] to fetch them."
            )
    else:
        print(" | ".join(columns))
        for row in rows_out:
            print(" | ".join(row))
        total_fetch = sum(s.cover_missing + s.shot_missing for s in all_stats)
        if execute:
            fetched = sum(s.cover_fetched + s.shot_fetched for s in all_stats)
            print(f"\nEXECUTE complete — {fetched} file(s) downloaded.")
        else:
            print(f"\nDRY RUN — {total_fetch} file(s) would be downloaded. Pass --execute to fetch them.")


def _load_env(config: dict[str, object]) -> None:
    config_dir = Path(str(config.get("_config_dir", Path.cwd())))
    for candidate in (config_dir / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            return
    load_dotenv()
