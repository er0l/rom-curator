"""ROMM metadata sync — fetch from the ROMM API and cache in inventory.sqlite."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .database import InventoryDatabase
from .mappings import find_canonical_system

try:
    import httpx
except ImportError as exc:
    raise RuntimeError(
        "httpx is required for romm-sync. Run: pip install -r curator/requirements.txt"
    ) from exc

try:
    from dotenv import load_dotenv
except ImportError as exc:
    raise RuntimeError(
        "python-dotenv is required for romm-sync. Run: pip install -r curator/requirements.txt"
    ) from exc

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
except ImportError:  # pragma: no cover
    Console = None
    Progress = None


@dataclass(frozen=True)
class RommSyncSummary:
    fetched: int
    upserted: int
    unresolved_platforms: int
    database_path: Path


BATCH_SIZE = 500


def run_romm_sync(
    config: dict[str, object],
    mappings: dict[str, dict[str, object]],
    *,
    reset: bool = False,
) -> RommSyncSummary:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    romm_config = config.get("romm") or {}
    if not isinstance(romm_config, dict):
        raise ValueError("Config key 'romm' must be a mapping")

    _load_env(config)

    romm_url = str(os.environ.get("ROMM_URL") or romm_config.get("url") or "").rstrip("/")
    romm_token = os.environ.get("ROMM_TOKEN", "")
    if not romm_url:
        raise ValueError(
            "ROMM URL not configured. Add ROMM_URL=https://your-romm-instance to .env"
        )
    if not romm_token:
        raise ValueError("ROMM token not found. Set ROMM_TOKEN in .env")

    page_size = int(romm_config.get("page_size", 200))
    database_path = Path(str(paths["database"])).expanduser()

    slug_to_canonical = _build_slug_index(mappings)
    console = Console() if Console else None
    headers = {"Authorization": f"Bearer {romm_token}", "Accept": "application/json"}

    if console:
        console.print(f"ROMM URL:  [bold]{romm_url}[/bold]")
        console.print(f"Database:  [bold]{database_path}[/bold]")
    else:
        print(f"ROMM URL:  {romm_url}")
        print(f"Database:  {database_path}")

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        _check_connectivity(client, romm_url)

        with InventoryDatabase(database_path) as db:
            db.initialize()
            if reset:
                db.clear_romm_roms()
                if console:
                    console.print("romm_roms table cleared.")
                else:
                    print("romm_roms table cleared.")

            fetched, upserted, unresolved = _sync_pages(
                client, db, romm_url, page_size, slug_to_canonical, console
            )

    summary = RommSyncSummary(
        fetched=fetched,
        upserted=upserted,
        unresolved_platforms=unresolved,
        database_path=database_path,
    )
    _print_summary(summary, console)
    return summary


def _sync_pages(
    client: httpx.Client,
    db: InventoryDatabase,
    romm_url: str,
    page_size: int,
    slug_to_canonical: dict[str, str],
    console,
) -> tuple[int, int, int]:
    synced_at = int(time.time())
    fetched = upserted = 0
    unresolved_slugs: set[str] = set()

    first_items, total = _fetch_page(client, romm_url, 0, page_size)
    if not first_items:
        return 0, 0, 0

    def _process_batch(items: list) -> None:
        nonlocal upserted
        for rom in items:
            record = _flatten_rom(rom, slug_to_canonical, synced_at)
            if record["canonical_system"] is None:
                unresolved_slugs.add(str(record["platform_slug"] or ""))
            db.upsert_romm_rom(record)
            upserted += 1

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
            task = progress.add_task("Syncing ROMM metadata", total=total)
            _process_batch(first_items)
            fetched += len(first_items)
            progress.advance(task, len(first_items))
            if fetched % BATCH_SIZE == 0:
                db.commit()

            offset = fetched
            while total is None or offset < total:
                items, _ = _fetch_page_with_retry(client, romm_url, offset, page_size)
                if not items:
                    break
                _process_batch(items)
                fetched += len(items)
                offset += len(items)
                progress.advance(task, len(items))
                if fetched % BATCH_SIZE == 0:
                    db.commit()
                if len(items) < page_size:
                    break
    else:
        _process_batch(first_items)
        fetched += len(first_items)
        offset = fetched
        while total is None or offset < total:
            items, _ = _fetch_page_with_retry(client, romm_url, offset, page_size)
            if not items:
                break
            _process_batch(items)
            fetched += len(items)
            offset += len(items)
            if fetched % BATCH_SIZE == 0:
                db.commit()
            if len(items) < page_size:
                break

    db.commit()
    return fetched, upserted, len(unresolved_slugs)


def _flatten_rom(
    rom: dict,
    slug_to_canonical: dict[str, str],
    synced_at: int,
) -> dict[str, object]:
    igdb = rom.get("igdb_metadata") or {}
    md = rom.get("metadatum") or {}
    hltb = rom.get("hltb_metadata") or {}
    if not isinstance(hltb, dict):
        hltb = {}

    platform_slug = rom.get("platform_slug") or ""
    canonical_system = slug_to_canonical.get(platform_slug)

    genres = _first(igdb.get("genres"), md.get("genres"), rom.get("genres")) or []
    themes = _first(igdb.get("themes"), md.get("themes")) or []
    modes = _first(igdb.get("game_modes"), md.get("game_modes")) or []

    release = igdb.get("first_release_date") or rom.get("first_release_date")

    return {
        "romm_id": rom.get("id"),
        "platform_slug": platform_slug or None,
        "canonical_system": canonical_system,
        "fs_name": rom.get("fs_name"),
        "fs_stem": _stem(rom.get("fs_name")),
        "name": rom.get("name"),
        "total_rating": igdb.get("total_rating"),
        "aggregated_rating": igdb.get("aggregated_rating"),
        "igdb_id": rom.get("igdb_id"),
        "is_identified": int(bool(rom.get("is_identified"))),
        "genres": _join_names(genres),
        "themes": _join_names(themes),
        "game_modes": _join_names(modes),
        "player_count": md.get("player_count"),
        "year": _to_year(release),
        "hltb_main": hltb.get("main_story") or hltb.get("comp_main"),
        "hltb_main_extra": hltb.get("main_extra") or hltb.get("comp_plus"),
        "hltb_completionist": hltb.get("completionist") or hltb.get("comp_100"),
        "sibling_count": len(rom.get("siblings") or []),
        "has_cover": int(bool(
            rom.get("path_cover_large") or rom.get("path_cover_small") or rom.get("url_cover")
        )),
        "regions": _join_names(rom.get("regions") or []),
        "tags": _join_names(rom.get("tags") or []),
        "synced_at": synced_at,
    }


def _fetch_page(
    client: httpx.Client,
    romm_url: str,
    offset: int,
    limit: int,
) -> tuple[list, int | None]:
    r = client.get(
        f"{romm_url}/api/roms",
        params={"limit": limit, "offset": offset, "order_by": "id", "order_dir": "asc"},
        timeout=60,
    )
    if r.status_code == 401:
        raise ValueError("ROMM auth failed (401). Check ROMM_TOKEN in .env")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        items = data.get("items") or data.get("data") or []
        total = data.get("total") or data.get("count")
    else:
        items = data
        total = None
    return items, total


def _fetch_page_with_retry(
    client: httpx.Client,
    romm_url: str,
    offset: int,
    limit: int,
) -> tuple[list, int | None]:
    try:
        return _fetch_page(client, romm_url, offset, limit)
    except httpx.HTTPError:
        time.sleep(5)
        return _fetch_page(client, romm_url, offset, limit)


def _check_connectivity(client: httpx.Client, romm_url: str) -> None:
    try:
        r = client.get(f"{romm_url}/api/heartbeat", timeout=10)
        if r.status_code == 401:
            raise ValueError("ROMM auth failed on /api/heartbeat. Check ROMM_TOKEN in .env")
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Cannot reach ROMM at {romm_url}: {exc}") from exc


def _build_slug_index(mappings: dict[str, dict[str, object]]) -> dict[str, str]:
    """Build reverse lookup: romm platform_slug → canonical system name."""
    index: dict[str, str] = {}
    for canonical, row in mappings.items():
        if not isinstance(row, dict):
            continue
        romm_aliases = row.get("romm")
        if isinstance(romm_aliases, list):
            for alias in romm_aliases:
                if alias:
                    index[str(alias)] = canonical
        elif isinstance(romm_aliases, str) and romm_aliases:
            index[romm_aliases] = canonical
    return index


def _load_env(config: dict[str, object]) -> None:
    config_dir = Path(str(config.get("_config_dir", Path.cwd())))
    for candidate in (config_dir / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            return
    load_dotenv()


def _print_summary(summary: RommSyncSummary, console) -> None:
    lines = [
        "ROMM sync complete",
        f"Fetched:              {summary.fetched}",
        f"Upserted:             {summary.upserted}",
        f"Unresolved platforms: {summary.unresolved_platforms}",
    ]
    if summary.unresolved_platforms:
        lines.append(
            "  (platform_slug not in systems.yaml romm: entries — canonical_system stored as NULL)"
        )
    if console:
        console.print("\n".join(lines), style="green")
    else:
        print("\n".join(lines))


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def _join_names(items) -> str | None:
    if not items:
        return None
    parts = []
    for item in items:
        if isinstance(item, dict):
            v = item.get("name")
            if v:
                parts.append(str(v))
        elif isinstance(item, str):
            parts.append(item)
    return "; ".join(parts) if parts else None


def _stem(filename: str | None) -> str | None:
    if not filename:
        return None
    dot = filename.rfind(".")
    return filename[:dot] if dot > 0 else filename


def _to_year(release) -> int | None:
    if not release:
        return None
    try:
        if isinstance(release, (int, float)):
            return int(time.strftime("%Y", time.gmtime(release)))
        s = str(release)
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
    except Exception:
        pass
    return None
