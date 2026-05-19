"""Compare a source ROM folder against a target folder to identify duplicates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None

ROM_EXTENSIONS: frozenset[str] = frozenset({
    "zip", "7z", "gz", "rar",
    "chd", "iso", "bin", "cue", "img", "nrg",
    "rom", "gb", "gbc", "gba", "nes", "sfc", "smc",
    "n64", "z64", "v64", "ndd",
    "nds", "3ds",
    "pce", "sgx",
    "gg", "sms", "gen", "md",
    "ws", "wsc",
    "ngp", "ngc",
    "lnx",
    "vb",
    "psx", "pbp",
    "xml",
})


@dataclass
class FolderCheckResult:
    source: Path
    target: Path
    # filename → (source_path, source_size, target_path, target_size)
    matched: list[tuple[str, Path, int, Path, int]] = field(default_factory=list)
    # same name, different size
    size_mismatch: list[tuple[str, Path, int, Path, int]] = field(default_factory=list)
    # only in source
    missing_from_target: list[tuple[str, Path, int]] = field(default_factory=list)
    # only in target (informational — not printed unless --detail)
    only_in_target: list[tuple[str, Path, int]] = field(default_factory=list)


def _index_folder(folder: Path, extensions: frozenset[str] | None) -> dict[str, tuple[Path, int]]:
    """Return {lowercase_filename: (path, size)} for every ROM file under folder."""
    index: dict[str, tuple[Path, int]] = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lstrip(".").lower()
        if extensions and ext not in extensions:
            continue
        key = path.name.lower()
        if key not in index:
            index[key] = (path, path.stat().st_size)
    return index


def run_folder_check(
    source: Path,
    target: Path,
    *,
    extensions: frozenset[str] | None = ROM_EXTENSIONS,
    detail: bool = False,
) -> FolderCheckResult:
    """Compare source folder against target and categorise every file."""
    if not source.is_dir():
        raise NotADirectoryError(f"Source is not a directory: {source}")
    if not target.is_dir():
        raise NotADirectoryError(f"Target is not a directory: {target}")

    console = Console() if Console else None

    if console:
        console.print(f"Indexing source: [bold]{source}[/bold]")
    else:
        print(f"Indexing source: {source}")

    src_index = _index_folder(source, extensions)

    if console:
        console.print(f"Indexing target: [bold]{target}[/bold]")
    else:
        print(f"Indexing target: {target}")

    tgt_index = _index_folder(target, extensions)

    result = FolderCheckResult(source=source, target=target)

    for name_lc, (src_path, src_size) in sorted(src_index.items()):
        if name_lc in tgt_index:
            tgt_path, tgt_size = tgt_index[name_lc]
            if src_size == tgt_size:
                result.matched.append((src_path.name, src_path, src_size, tgt_path, tgt_size))
            else:
                result.size_mismatch.append((src_path.name, src_path, src_size, tgt_path, tgt_size))
        else:
            result.missing_from_target.append((src_path.name, src_path, src_size))

    for name_lc, (tgt_path, tgt_size) in sorted(tgt_index.items()):
        if name_lc not in src_index:
            result.only_in_target.append((tgt_path.name, tgt_path, tgt_size))

    _print_result(result, detail=detail, console=console)
    return result


def _fmt_size(size: int) -> str:
    for unit, threshold in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if size >= threshold:
            return f"{size / threshold:.1f}{unit}"
    return f"{size}B"


def _print_result(result: FolderCheckResult, *, detail: bool, console) -> None:
    src_total = len(result.matched) + len(result.size_mismatch) + len(result.missing_from_target)
    tgt_total = len(result.matched) + len(result.size_mismatch) + len(result.only_in_target)

    lines = [
        "",
        f"Source : {result.source}  ({src_total} files)",
        f"Target : {result.target}  ({tgt_total} files)",
        "",
        f"  ✓  Already in target (same name + size) : {len(result.matched):>6}  — safe to delete from source",
        f"  ⚠  Name match, different size           : {len(result.size_mismatch):>6}  — different ROM version, keep both",
        f"  ✗  Not in target                        : {len(result.missing_from_target):>6}  — only in source",
        "",
    ]

    if result.matched:
        pct = 100 * len(result.matched) // src_total
        lines.append(f"Safe to delete: {len(result.matched)} / {src_total} files ({pct}%)")
    if result.size_mismatch:
        lines.append(f"Keep (CRC mismatch risk): {len(result.size_mismatch)} files")
    if result.missing_from_target:
        total_missing_size = sum(s for _, _, s in result.missing_from_target)
        lines.append(f"Missing from target: {len(result.missing_from_target)} files ({_fmt_size(total_missing_size)})")

    text = "\n".join(lines)
    if console:
        console.print(text)
    else:
        print(text)

    if detail or result.size_mismatch:
        if result.size_mismatch:
            _print_file_list(
                "⚠  Size mismatches (different ROM version — do NOT overwrite)",
                [(name, src, src_sz, tgt, tgt_sz) for name, src, src_sz, tgt, tgt_sz in result.size_mismatch],
                show_sizes=True,
                console=console,
            )
        if detail and result.missing_from_target:
            _print_file_list(
                "✗  Missing from target",
                [(name, src, src_sz, None, None) for name, src, src_sz in result.missing_from_target],
                show_sizes=False,
                console=console,
            )
        if detail and result.matched:
            _print_file_list(
                "✓  Safe to delete from source",
                [(name, src, src_sz, tgt, tgt_sz) for name, src, src_sz, tgt, tgt_sz in result.matched],
                show_sizes=False,
                console=console,
            )


def _print_file_list(
    heading: str,
    rows: list[tuple[str, Path, int, Path | None, int | None]],
    *,
    show_sizes: bool,
    console,
) -> None:
    if console and Table:
        table = Table(title=heading, show_lines=False, header_style="bold")
        table.add_column("File", style="cyan")
        if show_sizes:
            table.add_column("Source size", justify="right")
            table.add_column("Target size", justify="right")
        for name, _src, src_sz, _tgt, tgt_sz in rows:
            if show_sizes:
                table.add_row(name, _fmt_size(src_sz), _fmt_size(tgt_sz) if tgt_sz is not None else "-")
            else:
                table.add_row(name)
        console.print(table)
    else:
        print(f"\n{heading}:")
        for name, _src, src_sz, _tgt, tgt_sz in rows:
            if show_sizes:
                print(f"  {name}  ({_fmt_size(src_sz)} vs {_fmt_size(tgt_sz)})")
            else:
                print(f"  {name}")
