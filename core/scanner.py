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
    ignore_hidden: bool = True,
    follow_symlinks: bool = False,
    excluded_extensions: frozenset[str] = frozenset(),
) -> "Iterator[ScannedFile]":
    """Yield ScannedFile records.

    Callers can stream records into
    SQLite instead of building a giant in-memory list for a multi-terabyte tree.
    """
    root = Path(roms_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"ROM root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"ROM root is not a directory: {root}")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        if ignore_hidden:
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            filenames = [name for name in filenames if not name.startswith(".")]

        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
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
            system = parts[0] if len(parts) > 1 else "_root"
            extension = path.suffix.lower().lstrip(".")
            modified = int(stat.st_mtime)
            scan_key = f"{stat.st_size}:{modified}"

            yield ScannedFile(
                system=system,
                filename=filename,
                extension=extension,
                path=str(path),
                relative_path=str(relative_path),
                size=stat.st_size,
                modified=modified,
                scan_key=scan_key,
            )


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
