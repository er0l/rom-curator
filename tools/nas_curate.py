"""NAS curation based on what was deleted from a synced device.

After syncing ROMs to a device and playing them, you may delete games you
don't enjoy.  This tool compares the device's current ROM set against the
NAS export that was synced to it, finds the games that are now missing from
the device, and lets you interactively decide which ones to move to the NAS
recycle bin.

Usage::

    # Dry-run: show candidates without moving anything
    python3 romcurator.py nas-curate r36s --source root@192.168.1.100:/recalbox/share/roms
    python3 romcurator.py nas-curate r36s --source /run/media/erol/SDCARD/roms

    # Interactive prompt + move to recycle bin
    python3 romcurator.py nas-curate r36s --source root@192.168.1.100:/path --execute

The tool never touches the device — it only moves files on the NAS.

Algorithm
---------
1. List ROM files in the export directory (``paths.exports/<profile>/``).
2. List ROM files on the device at ``--source`` (local scan or SSH ``find``).
3. Files present in export but absent from device = deletion candidates.
4. For each candidate, show title + metadata and ask:
       [y] move to recycle bin
       [n] keep on NAS
       [a] move ALL remaining candidates
       [q] quit — keep all remaining
5. Confirmed candidates are moved from ``paths.roms/…`` to the recycle bin.

The export directory must exist (run ``build <profile> --execute`` first).
The device listing uses relative paths (``<system>/<filename>``) so export
and device structures are compared correctly regardless of their root paths.
"""

from __future__ import annotations

import os
import errno
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from core.database import InventoryDatabase

try:
    from rich.console import Console
    from rich.panel import Panel
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]
    Panel = None    # type: ignore[assignment,misc]


# ROM file extensions to consider when scanning the device.
_ROM_EXTENSIONS: frozenset[str] = frozenset({
    ".zip", ".7z", ".chd", ".iso", ".cue", ".img", ".bin",
    ".nes", ".sfc", ".smc", ".gba", ".gbc", ".gb",
    ".md", ".smd", ".gen",
    ".n64", ".z64", ".v64",
    ".nds", ".3ds",
    ".pce", ".tg16",
    ".gg", ".sms",
    ".ngc", ".neo",
    ".pgm", ".ips",
    ".lnx", ".atx",
    ".a26", ".a52", ".a78",
    ".rom", ".mx1", ".mx2",
    ".vec", ".col",
    ".ws", ".wsc",
    ".psx", ".ps2",
    ".m3u",
})


@dataclass
class CurateCandidate:
    rel_path: str      # relative path in export, e.g. "arcade/1942.zip"
    system: str        # system name from export structure
    filename: str      # bare filename, e.g. "1942.zip"
    title: str         # parsed title from DB (fallback: filename stem)
    year: str | None
    genre: str | None
    developer: str | None
    nas_path: Path | None   # absolute path on NAS (None if not found on disk)


@dataclass
class NasCurateSummary:
    profile: str
    source: str
    total_candidates: int = 0
    moved: int = 0
    kept: int = 0
    skipped_not_on_nas: int = 0
    errors: int = 0
    dry_run: bool = True


def run_nas_curate(
    config: dict[str, object],
    profile: str,
    source: str,
    *,
    systems: list[str] | None = None,
    execute: bool = False,
) -> NasCurateSummary:
    """Find ROMs deleted from *source* device and offer to move them to the NAS recycle bin.

    Parameters
    ----------
    config:
        Curator config dict.
    profile:
        Profile name — sub-directory under paths.exports used as reference.
    source:
        Where the device's ROMs currently live.  Either a local path
        (``/run/media/erol/SDCARD/roms``) or an SSH target
        (``user@host:/recalbox/share/roms``).
    systems:
        If given, only compare and curate these system names.  Other systems
        are ignored even if they appear in the export or on the device.
    execute:
        If False (default) no files are moved — just print the candidate list.
        If True, enter the interactive Y/N prompt.
    """
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    exports_root  = Path(str(paths.get("exports", "/mnt/storage/exports"))).expanduser()
    roms_root     = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()
    recycle_bin   = Path(str(paths.get("recycle_bin", "/mnt/storage/recycle_bin"))).expanduser()

    export_dir = exports_root / profile
    if not export_dir.is_dir():
        raise FileNotFoundError(
            f"Export directory not found: {export_dir}\n"
            f"Run 'build {profile} --execute' first."
        )
    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database not found: {database_path}")

    console = Console() if Console else None
    summary = NasCurateSummary(profile=profile, source=source, dry_run=not execute)

    scope = ", ".join(systems) if systems else "all systems"
    _print(console, f"Profile:   [bold]{profile}[/bold]" if console else f"Profile:   {profile}")
    _print(console, f"Scope:     {scope}")
    _print(console, f"Export:    {export_dir}")
    _print(console, f"Source:    {source}")
    _print(console, f"Mode:      {'EXECUTE (interactive)' if execute else 'DRY RUN (listing only)'}")
    _print(console, "")

    # Build the set of system names to consider (lower-cased for comparison).
    systems_filter: set[str] | None = {s.lower() for s in systems} if systems else None

    # Step 1 — collect relative ROM paths from the export, optionally filtered.
    _print(console, "Scanning export directory…", style="dim")
    export_files: set[str] = _list_local_files(export_dir, systems_filter)
    _print(console, f"  Export:  {len(export_files)} ROM files")

    # Step 2 — collect relative ROM paths from the device, optionally filtered.
    _print(console, "Scanning device…", style="dim")
    try:
        device_files: set[str] = _list_device_files(source, systems_filter)
    except Exception as exc:
        raise RuntimeError(f"Could not list files on device '{source}': {exc}") from exc
    _print(console, f"  Device:  {len(device_files)} ROM files")

    # Step 3 — diff: in export but not on device.
    missing = sorted(export_files - device_files)
    summary.total_candidates = len(missing)
    _print(console, f"  Missing from device: {len(missing)}\n")

    if not missing:
        _print(console, "No deleted ROMs found — device matches the export.", style="green")
        return summary

    # Step 4 — enrich candidates with DB metadata.
    candidates = _build_candidates(missing, roms_root, database_path)

    if not execute:
        # Dry-run: just print the list grouped by system.
        _print_candidate_list(candidates, console)
        _print(
            console,
            f"\nDRY RUN — {len(candidates)} candidate(s) found. "
            "Pass --execute to enter the interactive prompt.",
            style="bold",
        )
        return summary

    # Step 5 — interactive prompt.
    _print(console, "")
    move_all = False
    for idx, cand in enumerate(candidates, 1):
        summary.total_candidates = len(candidates)

        if cand.nas_path is None:
            _print(
                console,
                f"  [{cand.system}] {cand.filename}  — not found on NAS, skipping",
                style="yellow",
            )
            summary.skipped_not_on_nas += 1
            continue

        if move_all:
            _do_move(cand, recycle_bin, summary, console)
            continue

        # Print game card.
        _print_candidate_card(cand, idx, len(candidates), console)

        while True:
            try:
                answer = input("  Move to NAS recycle bin? [y]es / [n]o / [a]ll yes / [q]uit: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _print(console, "\nAborted — keeping all remaining.", style="yellow")
                return summary

            if answer in ("y", "yes"):
                _do_move(cand, recycle_bin, summary, console)
                break
            elif answer in ("n", "no"):
                summary.kept += 1
                _print(console, "  → kept", style="dim")
                break
            elif answer in ("a", "all"):
                move_all = True
                _do_move(cand, recycle_bin, summary, console)
                break
            elif answer in ("q", "quit"):
                remaining = len(candidates) - idx
                _print(
                    console,
                    f"  Quit — keeping {remaining} remaining candidate(s).",
                    style="yellow",
                )
                summary.kept += remaining
                return summary
            else:
                _print(console, "  Please enter y, n, a, or q.", style="dim")

    _print_summary(summary, recycle_bin, console)
    return summary


# ---------------------------------------------------------------------------
# File listing helpers
# ---------------------------------------------------------------------------

def _list_local_files(
    root: Path,
    systems_filter: set[str] | None = None,
) -> set[str]:
    """Return relative ROM paths under *root*, e.g. {'arcade/1942.zip'}.

    *systems_filter* — if provided, only include paths whose first component
    (the system directory name) is in the set (case-insensitive).
    """
    result: set[str] = set()
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # The first part of the relative path is the system folder.
        top = rel_dir.parts[0] if rel_dir.parts else ""
        if systems_filter and top.lower() not in systems_filter:
            continue
        for fname in files:
            if Path(fname).suffix.lower() in _ROM_EXTENSIONS:
                rel = str(rel_dir / fname)
                result.add(rel)
    return result


def _list_device_files(
    source: str,
    systems_filter: set[str] | None = None,
) -> set[str]:
    """Return relative ROM paths on *source* (local or SSH).

    For SSH sources (``user@host:/path``) we run ``find`` over SSH.
    For local paths we walk the directory directly.
    *systems_filter* is passed through to filter by system directory name.
    """
    if ":" in source and not source.startswith("/"):
        return _list_ssh_files(source, systems_filter)
    return _list_local_files(Path(source).expanduser(), systems_filter)


def _list_ssh_files(
    ssh_source: str,
    systems_filter: set[str] | None = None,
) -> set[str]:
    """List files on a remote device via SSH find.

    *ssh_source* format: ``user@host:/remote/path``
    *systems_filter* — when given, ``find`` is scoped to those sub-directories
    only, which is faster than listing everything and filtering in Python.
    """
    colon = ssh_source.index(":")
    user_host = ssh_source[:colon]
    remote_path = ssh_source[colon + 1:].rstrip("/")

    # Build extension pattern for find -name.
    find_name_parts: list[str] = []
    for ext in sorted(_ROM_EXTENSIONS):
        find_name_parts += ["-o", "-name", f"*{ext}"]
    find_name_parts = find_name_parts[2:]  # remove leading -o

    if systems_filter:
        # Run find inside each system sub-directory separately.
        all_lines: list[str] = []
        for sys_name in sorted(systems_filter):
            sys_path = f"{remote_path}/{sys_name}"
            cmd = ["ssh", user_host, "find", sys_path, "-type", "f",
                   r"\(", *find_name_parts, r"\)"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode not in (0, 1):
                raise RuntimeError(
                    f"SSH find failed for {sys_path} (exit {r.returncode}): {r.stderr.strip()}"
                )
            all_lines.extend(r.stdout.splitlines())
    else:
        cmd = ["ssh", user_host, "find", remote_path, "-type", "f",
               r"\(", *find_name_parts, r"\)"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode not in (0, 1):
            raise RuntimeError(
                f"SSH find failed (exit {r.returncode}): {r.stderr.strip()}"
            )
        all_lines = r.stdout.splitlines()

    prefix = remote_path + "/"
    rel_paths: set[str] = set()
    for line in all_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith(prefix):
            line = line[len(prefix):]
        rel_paths.add(line)
    return rel_paths


# ---------------------------------------------------------------------------
# Candidate building
# ---------------------------------------------------------------------------

def _build_candidates(
    missing_rel: list[str],
    roms_root: Path,
    database_path: Path,
) -> list[CurateCandidate]:
    """Enrich missing-file entries with DB metadata and NAS path."""
    # Build a {system: {filename: row}} lookup from DB.
    systems_needed = {rel.split("/")[0] for rel in missing_rel if "/" in rel}

    meta: dict[str, dict[str, dict]] = {}  # system → filename → row
    with InventoryDatabase(database_path) as db:
        db.initialize()
        for system in systems_needed:
            rows = db.fetch_all(
                """
                SELECT r.filename, r.title,
                       rr.year, rr.genres, rr.developer,
                       mm.year AS mame_year, mm.manufacturer
                FROM roms r
                LEFT JOIN romm_roms rr
                    ON rr.canonical_system = r.system
                    AND rr.fs_name = r.filename
                LEFT JOIN mame_machines mm
                    ON mm.name = r.title
                    AND r.system IN ('arcade', 'mame2003-plus')
                WHERE r.system = ?
                """,
                (system,),
            )
            meta[system] = {str(row["filename"]): row for row in rows}

    candidates: list[CurateCandidate] = []
    for rel in missing_rel:
        parts = rel.split("/", 1)
        if len(parts) != 2:
            continue
        system, filename = parts

        row = meta.get(system, {}).get(filename)
        if row:
            title = str(row["title"])
            year = str(row["year"] or row["mame_year"] or "") or None
            genre = str(row["genres"]).split(";")[0].strip() if row["genres"] else None
            developer = (
                str(row["developer"]).split(";")[0].strip()
                if row["developer"] else
                (str(row["manufacturer"]) if row["manufacturer"] else None)
            )
        else:
            title = Path(filename).stem
            year = genre = developer = None

        nas_path = roms_root / system / filename
        if not nas_path.exists():
            nas_path = None  # type: ignore[assignment]

        candidates.append(CurateCandidate(
            rel_path=rel,
            system=system,
            filename=filename,
            title=title,
            year=year,
            genre=genre,
            developer=developer,
            nas_path=nas_path,
        ))

    return candidates


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_candidate_list(candidates: list[CurateCandidate], console) -> None:
    last_system = ""
    for cand in candidates:
        if cand.system != last_system:
            _print(console, f"\n  [{cand.system}]", style="bold")
            last_system = cand.system
        parts = [f"    {cand.filename}"]
        if cand.title != Path(cand.filename).stem:
            parts.append(f'"{cand.title}"')
        extras = [x for x in (cand.year, cand.genre, cand.developer) if x]
        if extras:
            parts.append(f"({' • '.join(extras)})")
        if cand.nas_path is None:
            parts.append("[not found on NAS]")
        _print(console, "  ".join(parts))


def _print_candidate_card(
    cand: CurateCandidate,
    idx: int,
    total: int,
    console,
) -> None:
    extras = [x for x in (cand.year, cand.genre, cand.developer) if x]
    meta_str = f"  ({' • '.join(extras)})" if extras else ""
    header = f"[{idx}/{total}]  [{cand.system}]  {cand.filename}"
    body   = f'  "{cand.title}"{meta_str}'
    if console and Panel:
        console.print(Panel(f"[bold]{cand.title}[/bold]{meta_str}",
                            title=f"[dim]{idx}/{total}[/dim]  [{cand.system}]  {cand.filename}",
                            expand=False))
    else:
        print(f"\n{header}")
        print(body)


def _print_summary(summary: NasCurateSummary, recycle_bin: Path, console) -> None:
    parts = [
        f"\nDone — moved: {summary.moved}  kept: {summary.kept}"
        + (f"  not-on-NAS: {summary.skipped_not_on_nas}" if summary.skipped_not_on_nas else "")
        + (f"  errors: {summary.errors}" if summary.errors else "")
    ]
    style = "red" if summary.errors else "green"
    _print(console, parts[0], style=style)
    if summary.moved:
        _print(console, f"Recycle bin: {recycle_bin / 'roms'}", style="dim")


# ---------------------------------------------------------------------------
# Move helper
# ---------------------------------------------------------------------------

def _do_move(
    cand: CurateCandidate,
    recycle_bin: Path,
    summary: NasCurateSummary,
    console,
) -> None:
    assert cand.nas_path is not None
    dest = recycle_bin / "roms" / cand.system / cand.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _safe_move(cand.nas_path, dest)
        summary.moved += 1
        _print(console, f"  → moved to recycle bin", style="green")
    except Exception as exc:
        summary.errors += 1
        _print(console, f"  ERROR moving {cand.filename}: {exc}", style="red")


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


# ---------------------------------------------------------------------------
# Print helper
# ---------------------------------------------------------------------------

def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        import re
        plain = re.sub(r"\[/?[^\[\]]*\]", "", msg)
        print(plain)
