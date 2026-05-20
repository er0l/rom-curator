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
"""

from __future__ import annotations

import os
import time
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
except ImportError:
    Console = None
    Progress = None


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
    dry_run: bool = False,
    console=None,
) -> dict[str, int]:
    """Download missing cover and screenshot images for *system*.

    Returns stats: total, cover_fetched, cover_skipped, screenshot_fetched,
    screenshot_skipped, errors.
    """
    stats = {
        "total": 0,
        "cover_fetched": 0,
        "cover_skipped": 0,
        "screenshot_fetched": 0,
        "screenshot_skipped": 0,
        "errors": 0,
    }

    folder_name = nas_folder or system
    system_dir = roms_root / folder_name
    if not system_dir.is_dir():
        raise FileNotFoundError(f"System folder not found: {system_dir}")

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

    headers = {"Accept": "image/*, video/*"}
    romm_base = romm_url.rstrip("/")

    with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
        tasks = list(rows)
        stats["total"] = len(tasks)

        def _do_fetch(progress=None, task_id=None):
            for i, row in enumerate(tasks):
                stem = str(row["fs_stem"] or "")
                if not stem:
                    continue

                _fetch_asset(
                    client, romm_base,
                    url=row["url_cover"],
                    dest=system_dir / "boxart" / f"{stem}.jpg",
                    stats_key_fetched="cover_fetched",
                    stats_key_skipped="cover_skipped",
                    stats=stats,
                    dry_run=dry_run,
                )
                if delay > 0 and not dry_run:
                    time.sleep(delay)

                screenshots_raw = str(row["url_screenshots"] or "")
                first_screenshot = screenshots_raw.split(";")[0].strip() if screenshots_raw else None
                _fetch_asset(
                    client, romm_base,
                    url=first_screenshot,
                    dest=system_dir / "screenshots" / f"{stem}.jpg",
                    stats_key_fetched="screenshot_fetched",
                    stats_key_skipped="screenshot_skipped",
                    stats=stats,
                    dry_run=dry_run,
                )
                if delay > 0 and not dry_run:
                    time.sleep(delay)

                if progress is not None and task_id is not None:
                    progress.advance(task_id)

        if Progress and console:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
            )
            with progress:
                task_id = progress.add_task(f"Fetching media: {system}", total=len(tasks))
                _do_fetch(progress, task_id)
        else:
            _do_fetch()

    return stats


def _fetch_asset(
    client: httpx.Client,
    romm_base: str,
    url: str | None,
    dest: Path,
    stats_key_fetched: str,
    stats_key_skipped: str,
    stats: dict[str, int],
    dry_run: bool,
) -> None:
    if not url:
        return
    if dest.exists():
        stats[stats_key_skipped] += 1
        return
    if dry_run:
        stats[stats_key_fetched] += 1
        return

    full_url = url if url.startswith("http") else f"{romm_base}{url}"
    try:
        r = client.get(full_url)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        stats[stats_key_fetched] += 1
    except Exception:
        stats["errors"] += 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_fetch_media(
    config: dict[str, object],
    systems: list[str],
    mappings: dict[str, dict[str, object]],
    *,
    dry_run: bool = False,
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

    rows_out = []
    for system in systems:
        nas_folder = None
        sys_meta = mappings.get(system, {})
        if isinstance(sys_meta, dict) and sys_meta.get("nas"):
            nas_folder = str(sys_meta["nas"])

        folder_name = nas_folder or system
        system_dir  = roms_root / folder_name
        if not system_dir.is_dir():
            rows_out.append((system, folder_name, "—", "—", "—", "—", "folder not found"))
            continue

        try:
            stats = fetch_media(
                system, roms_root, database_path, romm_url,
                nas_folder=folder_name, delay=delay, dry_run=dry_run, console=console,
            )
            prefix = "DRY RUN" if dry_run else "done"
            rows_out.append((
                system,
                folder_name,
                str(stats["total"]),
                str(stats["cover_fetched"]),
                str(stats["cover_skipped"]),
                str(stats["screenshot_fetched"]),
                f"{prefix} ({stats['errors']} errors)" if stats["errors"] else prefix,
            ))
        except Exception as exc:
            rows_out.append((system, folder_name, "—", "—", "—", "—", f"ERROR: {exc}"))

    columns = ("System", "Folder", "ROMs", "Cover↓", "Cover✓", "Shot↓", "Status")
    if console:
        from rich.table import Table
        table = Table(title="fetch-media")
        for col in columns:
            table.add_column(col)
        for row in rows_out:
            table.add_row(*row)
        console.print(table)
    else:
        print(" | ".join(columns))
        for row in rows_out:
            print(" | ".join(row))


def _load_env(config: dict[str, object]) -> None:
    config_dir = Path(str(config.get("_config_dir", Path.cwd())))
    for candidate in (config_dir / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            return
    load_dotenv()
