"""Zip uncompressed ROM files in-place, deleting the originals after verification."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from rich.console import Console
except ImportError:  # pragma: no cover
    Console = None


# Single-file ROM formats safe to compress individually.
# CD-ROM formats (.bin/.cue, .iso, .img, .mdf/.mds) are intentionally excluded —
# they often involve companion files and need manual handling.
COMPRESSIBLE_EXTENSIONS: frozenset[str] = frozenset({
    ".nes", ".fds",
    ".sfc", ".smc",
    ".sms", ".gg",
    ".gb", ".gbc", ".gba",
    ".n64", ".z64", ".v64",
    ".md", ".gen", ".32x",
    ".pce", ".sg",
    ".col", ".lnx",
    ".ngp", ".ngc",
    ".ws", ".wsc",
    ".a26", ".a52", ".a78",
    ".nds",
    ".dsk", ".adf", ".d64",
    ".tap", ".tzx", ".sna", ".z80", ".sc", ".cas", ".prg", ".t64", ".crt",
})

ALREADY_COMPRESSED: frozenset[str] = frozenset({
    ".zip", ".7z", ".gz", ".rar", ".chd", ".cso", ".pbp", ".rvz", ".wia",
})


@dataclass
class ZipRomsSummary:
    system: str
    seen: int = 0
    already_compressed: int = 0
    would_zip: int = 0
    zipped: int = 0
    errors: int = 0
    dry_run: bool = True


def run_zip_roms(
    config: dict[str, object],
    *,
    system: str | None = None,
    execute: bool = False,
) -> list[ZipRomsSummary]:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root = Path(str(paths["roms"])).expanduser()
    console = Console() if Console else None

    system_folders = _resolve_system_folders(roms_root, system)
    if not system_folders:
        _print(console, f"No system folders found under {roms_root}")
        return []

    summaries: list[ZipRomsSummary] = []
    for folder in system_folders:
        summary = _zip_system(folder, roms_root, execute=execute, console=console)
        summaries.append(summary)

    _print_totals(summaries, execute, console)

    if execute:
        _print(
            console,
            "\nInventory is now stale — run 'inventory' to rescan the updated archive.",
            style="yellow",
        )

    return summaries


def _resolve_system_folders(roms_root: Path, system: str | None) -> list[Path]:
    if system:
        folder = roms_root / system
        if not folder.is_dir():
            raise FileNotFoundError(f"System folder not found: {folder}")
        return [folder]
    return sorted(p for p in roms_root.iterdir() if p.is_dir())


def _zip_system(
    sys_folder: Path,
    roms_root: Path,
    *,
    execute: bool,
    console,
) -> ZipRomsSummary:
    system_name = sys_folder.name
    summary = ZipRomsSummary(system=system_name, dry_run=not execute)
    plan: list[Path] = []

    for path in sorted(sys_folder.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in ALREADY_COMPRESSED:
            summary.seen += 1
            summary.already_compressed += 1
            continue
        if ext not in COMPRESSIBLE_EXTENSIONS:
            continue
        summary.seen += 1
        if path.with_suffix(".zip").exists():
            summary.already_compressed += 1
            continue
        plan.append(path)
        summary.would_zip += 1

    _print(console, f"\nSystem: {system_name}")
    _print(console, f"  Compressible: {summary.would_zip}  |  Already compressed: {summary.already_compressed}")
    _print(console, f"  Mode:         {'EXECUTE' if execute else 'DRY RUN'}")

    for rom_path in plan:
        zip_path = rom_path.with_suffix(".zip")
        rel = rom_path.relative_to(roms_root)

        if not execute:
            _print(console, f"  WOULD ZIP  {rel}  →  {zip_path.name}")
            continue

        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(rom_path, arcname=rom_path.name)
            # Verify the archive is intact before removing the original
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.read(rom_path.name)
            rom_path.unlink()
            summary.zipped += 1
            _print(console, f"  ZIPPED  {rel}  →  {zip_path.name}", style="green")
        except Exception as exc:
            summary.errors += 1
            zip_path.unlink(missing_ok=True)
            _print(console, f"  ERROR   {rel}: {exc}", style="red")

    return summary


def _print_totals(summaries: list[ZipRomsSummary], execute: bool, console) -> None:
    total_would = sum(s.would_zip for s in summaries)
    total_done = sum(s.zipped for s in summaries)
    total_err = sum(s.errors for s in summaries)

    if not execute:
        msg = (
            f"\nDRY RUN complete — {total_would} file(s) across {len(summaries)} system(s) "
            "would be zipped. Pass --execute to proceed."
        )
        _print(console, msg, style="bold")
    else:
        style = "red" if total_err else "green"
        _print(console, f"\nDone — zipped: {total_done}  errors: {total_err}", style=style)


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)
