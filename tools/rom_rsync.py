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
    export_root = exports_root / profile

    if not export_root.is_dir():
        raise FileNotFoundError(
            f"Export directory not found: {export_root}\n"
            f"Run 'build {profile} --execute' first to create it."
        )

    # Resolve which system sub-directories to sync.
    if systems:
        sync_systems = systems
        # Warn about systems not present in the export.
        missing = [s for s in sync_systems if not (export_root / s).is_dir()]
        if missing:
            raise FileNotFoundError(
                f"System(s) not found in export '{profile}': {', '.join(missing)}\n"
                f"Available: {', '.join(sorted(d.name for d in export_root.iterdir() if d.is_dir()))}"
            )
    else:
        sync_systems = None  # signal: sync the whole root

    console = Console() if Console else None
    dry_run = not execute

    scope = ", ".join(sync_systems) if sync_systems else "all systems"
    _print(console, f"Profile:   [bold]{profile}[/bold]" if console else f"Profile:   {profile}")
    _print(console, f"Scope:     {scope}")
    _print(console, f"Export:    {export_root}/")
    _print(console, f"Dest:      {dest}")
    _print(console, f"Delete:    {'yes' if delete else 'no'}")
    _print(console, f"Mode:      {'DRY RUN' if dry_run else 'EXECUTE'}")
    _print(console, "")

    base_cmd: list[str] = [
        "rsync",
        "--archive",        # -a: recursive + preserve permissions/times/links
        "--verbose",        # show transferred files
        "--progress",       # per-file transfer progress
        "--human-readable",
    ]
    if dry_run:
        base_cmd.append("--dry-run")
    if delete:
        base_cmd.append("--delete")
    if extra_rsync_args:
        base_cmd.extend(extra_rsync_args)

    worst_exit = 0

    def _run_rsync(src: str, dst: str) -> int:
        cmd = base_cmd + [src, dst]
        if console:
            console.print(f"[dim]rsync {src} → {dst}[/dim]")
        try:
            r = subprocess.run(cmd, check=False)
            return r.returncode
        except FileNotFoundError:
            raise RuntimeError(
                "rsync not found. Install it with: sudo apt install rsync  "
                "(or brew install rsync on macOS)"
            )

    if sync_systems:
        # Per-system invocations: rsync exports/<profile>/<sys>/ → dest/<sys>/
        dest_has_colon = ":" in dest and not dest.startswith("/")
        for sys_name in sync_systems:
            src = f"{export_root / sys_name}/"
            dst = f"{dest.rstrip('/')}/{sys_name}" if not dest_has_colon else \
                  f"{dest.rstrip('/')}/{sys_name}"
            code = _run_rsync(src, dst)
            if code > worst_exit:
                worst_exit = code
    else:
        # Whole-root invocation: rsync exports/<profile>/ → dest/
        src = f"{export_root}/"
        code = _run_rsync(src, dest)
        worst_exit = code

    summary = RsyncSummary(
        profile=profile,
        source=export_root,
        dest=dest,
        dry_run=dry_run,
        delete=delete,
        systems=sync_systems,
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
