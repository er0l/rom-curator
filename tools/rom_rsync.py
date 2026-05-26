"""Rsync a profile's hardlink export to a target device.

Wraps rsync to transfer the curated export built by 'build <profile>' to a
local mount point or a remote device reachable via SSH.

Usage examples::

    # Dry-run (shows what rsync would transfer, no files sent)
    python3 romcurator.py rom-rsync r36s --dest root@192.168.1.100:/recalbox/share/roms

    # Transfer for real
    python3 romcurator.py rom-rsync r36s --dest /run/media/erol/SDCARD/roms --execute

    # Transfer and remove files on the device that are no longer in the export
    python3 romcurator.py rom-rsync r36s --dest root@192.168.1.100:/path --delete --execute

The source is always the pre-built export directory for the named profile
(``paths.exports/<profile>/``).  Run ``build <profile> --execute`` first if
the export does not exist.

SSH connectivity uses the standard OpenSSH client — key auth, agent, and
~/.ssh/config entries all work normally.  No credentials are stored here.
"""

from __future__ import annotations

import subprocess
import sys
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
    exit_code: int = 0


def run_rom_rsync(
    config: dict[str, object],
    profile: str,
    dest: str,
    *,
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
    source = exports_root / profile

    if not source.is_dir():
        raise FileNotFoundError(
            f"Export directory not found: {source}\n"
            f"Run 'build {profile} --execute' first to create it."
        )

    console = Console() if Console else None
    dry_run = not execute

    _print(console, f"Profile:   [bold]{profile}[/bold]" if console else f"Profile:   {profile}")
    _print(console, f"Source:    {source}/")
    _print(console, f"Dest:      {dest}")
    _print(console, f"Delete:    {'yes' if delete else 'no'}")
    _print(console, f"Mode:      {'DRY RUN' if dry_run else 'EXECUTE'}")
    _print(console, "")

    cmd: list[str] = [
        "rsync",
        "--archive",       # -a: recursive + preserve permissions/times/links
        "--verbose",       # show transferred files
        "--progress",      # per-file transfer progress
        "--human-readable",
    ]
    if dry_run:
        cmd.append("--dry-run")
    if delete:
        cmd.append("--delete")
    if extra_rsync_args:
        cmd.extend(extra_rsync_args)

    # Trailing slash on source syncs *contents*, not the directory itself.
    cmd.append(f"{source}/")
    cmd.append(dest)

    if console:
        console.print(f"Command:   [dim]{' '.join(cmd)}[/dim]")
        console.print("")

    try:
        result = subprocess.run(cmd, check=False)
        exit_code = result.returncode
    except FileNotFoundError:
        raise RuntimeError(
            "rsync not found. Install it with: sudo apt install rsync  "
            "(or brew install rsync on macOS)"
        )

    summary = RsyncSummary(
        profile=profile,
        source=source,
        dest=dest,
        dry_run=dry_run,
        delete=delete,
        exit_code=exit_code,
    )

    if exit_code == 0:
        status = "DRY RUN complete" if dry_run else "Sync complete"
        _print(console, f"\n{status}.", style="bold green")
        if dry_run:
            _print(
                console,
                "Pass --execute to perform the actual transfer.",
                style="dim",
            )
    else:
        _print(
            console,
            f"\nrsync exited with code {exit_code}.",
            style="bold red",
        )
        if exit_code == 23:
            _print(console, "Hint: some files could not be transferred (permissions?).")
        elif exit_code == 255:
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
