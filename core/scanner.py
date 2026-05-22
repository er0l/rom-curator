"""Filesystem scanner for canonical ROM archives."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
import os


IGNORED_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_DIRS = {".git", "cache", "savestates"}
IGNORED_PREFIXES = ("._",)
IGNORED_EXTENSIONS = {".srm", ".state"}


@dataclass(frozen=True)
class ScannedFile:
    system: str
    filename: str
    extension: str
    path: str
    relative_path: str
    size: int
    modified: int
    scan_key: str


def iter_rom_files(
    roms_root: str | Path,
    *,
    system: str | None = None,
    canonical_system: str | None = None,
    nas_to_canonical: "dict[str, str] | None" = None,
    ignore_hidden: bool = True,
    follow_symlinks: bool = False,
    excluded_extensions: frozenset[str] = frozenset(),
    skip_subdirs: "frozenset[str]" = frozenset(),
) -> "Iterator[ScannedFile]":
    """Yield ScannedFile records.

    Callers can stream records into
    SQLite instead of building a giant in-memory list for a multi-terabyte tree.

    If *system* is given, only the ``roms_root/<system>/`` subtree is walked.
    *system* may be a subpath like ``arcade/mame2003-plus``.

    *canonical_system* overrides the system name stored in ScannedFile — use
    when the NAS folder path differs from the canonical system name (e.g. the
    nas path is ``arcade/mame2003-plus`` but the canonical name is
    ``mame2003-plus``).

    *nas_to_canonical* maps NAS subpath strings to canonical system names and
    is used during full-archive scans to correctly assign systems to files
    living in nested folders.  The longest matching prefix wins.

    *skip_subdirs* prunes these directory names from the walk — used to
    prevent a parent system scan from descending into child system folders.
    """
    root = Path(roms_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"ROM root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"ROM root is not a directory: {root}")

    if system:
        scan_root = root / system
        if not scan_root.exists():
            raise FileNotFoundError(f"System folder does not exist: {scan_root}")
        if not scan_root.is_dir():
            raise NotADirectoryError(f"System path is not a directory: {scan_root}")
    else:
        scan_root = root

    for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=follow_symlinks):
        if ignore_hidden:
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            filenames = [name for name in filenames if not name.startswith(".")]

        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
        if skip_subdirs:
            dirnames[:] = [name for name in dirnames if name not in skip_subdirs]
        dirnames.sort()
        for filename in sorted(filenames):
            if _should_ignore_file(filename, excluded_extensions):
                continue

            path = Path(dirpath) / filename
            try:
                stat = path.stat()
            except OSError:
                continue

            if not follow_symlinks and path.is_symlink():
                continue

            relative_path = path.relative_to(root)
            parts = relative_path.parts
            extension = path.suffix.lower().lstrip(".")
            modified = int(stat.st_mtime)
            scan_key = f"{stat.st_size}:{modified}"

            if canonical_system:
                assigned_system = canonical_system
            elif nas_to_canonical:
                assigned_system = _resolve_canonical(parts, nas_to_canonical)
            else:
                assigned_system = parts[0] if len(parts) > 1 else "_root"

            yield ScannedFile(
                system=assigned_system,
                filename=filename,
                extension=extension,
                path=str(path),
                relative_path=str(relative_path),
                size=stat.st_size,
                modified=modified,
                scan_key=scan_key,
            )


def _resolve_canonical(parts: tuple[str, ...], nas_to_canonical: dict[str, str]) -> str:
    """Return the canonical system name for a file by longest-prefix match.

    ``parts`` is the file's path components relative to roms_root.
    ``nas_to_canonical`` maps NAS folder paths (e.g. ``arcade/mame2003-plus``)
    to canonical system names.  The deepest (longest) matching prefix wins so
    that ``arcade/mame2003-plus/1942.zip`` resolves to ``mame2003-plus`` rather
    than ``arcade``.
    """
    best_canonical = parts[0] if parts else "_root"
    best_depth = 0
    folder = "/".join(parts[:-1])  # everything except the filename
    for nas_path, canonical in nas_to_canonical.items():
        nas_parts = nas_path.split("/")
        depth = len(nas_parts)
        if depth > best_depth and parts[:depth] == tuple(nas_parts):
            best_canonical = canonical
            best_depth = depth
    return best_canonical


def _should_ignore_file(filename: str, excluded_extensions: frozenset[str] = frozenset()) -> bool:
    if filename in IGNORED_FILENAMES:
        return True
    if filename.startswith(IGNORED_PREFIXES):
        return True
    ext_with_dot = Path(filename).suffix.lower()
    if ext_with_dot in IGNORED_EXTENSIONS:
        return True
    ext = ext_with_dot.lstrip(".")
    return ext in excluded_extensions
