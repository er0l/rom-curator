"""Rsync curated ROMs to a target device using per-system manifest files.

``build <profile> --execute`` writes a manifest into ``exports/<profile>/``:

* ``manifest.json``    — maps each system to its NAS folder and device folder
* ``<system>.files``  — file paths relative to the system's NAS folder

This tool reads the manifest and calls rsync with ``--files-from`` so only the
curated ROM files are transferred — no intermediate hardlink directory needed.

Usage examples::

    # Dry-run (shows what rsync would transfer, no files sent)
    python3 romcurator.py rom-rsync r36s --dest root@192.168.1.100:/recalbox/share/roms

    # Transfer for real
    python3 romcurator.py rom-rsync r36s --dest /run/media/erol/SDCARD/roms --execute

    # Transfer and remove files on the device that are no longer in the export
    python3 romcurator.py rom-rsync r36s --dest root@192.168.1.100:/path --delete --execute

SSH connectivity uses the standard OpenSSH client — key auth, agent, and
~/.ssh/config entries all work normally.  No credentials are stored here.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    from rich.console import Console
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]


@dataclass
class RsyncSummary:
    profile: str
    source: Path
    dest: str
    dry_run: bool
    delete: bool
    systems: list[str] | None = None
    exit_code: int = 0


def run_rom_rsync(
    config: dict[str, object],
    profile: str,
    dest: str,
    *,
    systems: list[str] | None = None,
    delete: bool = False,
    execute: bool = False,
    extra_rsync_args: list[str] | None = None,
) -> RsyncSummary:
    """Rsync *profile*'s export to *dest*.

    Parameters
    ----------
    config:
        Curator config dict (paths.exports must be set).
    profile:
        Profile name — must match a sub-directory under paths.exports.
    dest:
        rsync destination: a local path (``/run/media/…/roms``) or an SSH
        target (``user@host:/path``).
    systems:
        If given, only rsync these system sub-directories.  Each system is
        transferred with a separate rsync invocation so that ``--delete``
        applies per-system rather than across the whole dest root.
    delete:
        Pass ``--delete`` to rsync so the device mirrors the export exactly.
        Off by default — device may have extra files that should be kept.
    execute:
        If False (default) rsync runs in dry-run mode (``--dry-run``).
    extra_rsync_args:
        Additional raw rsync flags inserted before the source/dest pair.
    """
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    exports_root = Path(str(paths.get("exports", "/mnt/storage/exports"))).expanduser()
    roms_root    = Path(str(paths.get("roms", "/mnt/storage/roms"))).expanduser()
    export_dir   = exports_root / profile
    manifest_path = export_dir / "manifest.json"

    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {manifest_path}\n"
            f"Run 'build {profile} --execute' first to generate it."
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not read manifest {manifest_path}: {exc}") from exc

    all_systems_in_manifest: list[str] = sorted(manifest.get("systems", {}).keys())

    # Filter to requested systems.
    if systems:
        unknown = sorted(set(systems) - set(all_systems_in_manifest))
        if unknown:
            raise FileNotFoundError(
                f"System(s) not in manifest for profile '{profile}': {', '.join(unknown)}\n"
                f"Available: {', '.join(all_systems_in_manifest)}"
            )
        sync_systems = systems
    else:
        sync_systems = all_systems_in_manifest

    console = Console() if Console else None
    dry_run = not execute

    scope = ", ".join(sync_systems) if sync_systems != all_systems_in_manifest else "all systems"
    _print(console, f"Profile:   [bold]{profile}[/bold]" if console else f"Profile:   {profile}")
    _print(console, f"Scope:     {scope}")
    _print(console, f"Manifest:  {manifest_path}")
    _print(console, f"Dest:      {dest}")
    _print(console, f"Delete:    {'yes' if delete else 'no'}")
    _print(console, f"Mode:      {'DRY RUN' if dry_run else 'EXECUTE'}")
    _print(console, "")

    base_cmd: list[str] = [
        "rsync",
        "--archive",         # -a: recursive + preserve permissions/times/links
        "--verbose",         # show transferred files
        "--progress",        # per-file transfer progress
        "--human-readable",
    ]
    if dry_run:
        base_cmd.append("--dry-run")
    if delete:
        base_cmd.append("--delete")
    if extra_rsync_args:
        base_cmd.extend(extra_rsync_args)

    worst_exit = 0

    def _run_rsync(files_from: Path, src: str, dst: str) -> int:
        cmd = base_cmd + [f"--files-from={files_from}", src, dst]
        if console:
            console.print(f"[dim]rsync --files-from={files_from.name} {src} → {dst}[/dim]")
        try:
            r = subprocess.run(cmd, check=False)
            return r.returncode
        except FileNotFoundError:
            raise RuntimeError(
                "rsync not found. Install it with: sudo apt install rsync  "
                "(or brew install rsync on macOS)"
            )

    for sys_name in sync_systems:
        sys_meta = manifest["systems"][sys_name]
        nas_folder: str  = sys_meta["nas_folder"]    # e.g. "snes" or "arcade/mame2003-plus"
        device_folder: str = sys_meta["device_folder"]  # e.g. "snes" or "mame"
        files_path = export_dir / f"{sys_name}.files"

        if not files_path.exists():
            _print(console, f"  Warning: {files_path.name} not found — skipping {sys_name}", style="yellow")
            continue

        nas_src  = str(roms_root / nas_folder) + "/"
        dest_sys = f"{dest.rstrip('/')}/{device_folder}"

        code = _run_rsync(files_path, nas_src, dest_sys)
        if code > worst_exit:
            worst_exit = code

    summary = RsyncSummary(
        profile=profile,
        source=export_dir,
        dest=dest,
        dry_run=dry_run,
        delete=delete,
        systems=sync_systems if sync_systems != all_systems_in_manifest else None,
        exit_code=worst_exit,
    )

    if worst_exit == 0:
        status = "DRY RUN complete" if dry_run else "Sync complete"
        _print(console, f"\n{status}.", style="bold green")
        if dry_run:
            _print(console, "Pass --execute to perform the actual transfer.", style="dim")
    else:
        _print(console, f"\nrsync exited with code {worst_exit}.", style="bold red")
        if worst_exit == 23:
            _print(console, "Hint: some files could not be transferred (permissions?).")
        elif worst_exit == 255:
            _print(console, "Hint: SSH connection failed — check host, user, and key.")

    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        # Strip rich markup for plain output
        import re
        plain = re.sub(r"\[/?[^\[\]]*\]", "", msg)
        print(plain)
