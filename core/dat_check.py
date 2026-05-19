"""Compare a ROM folder against one or more MAME XML DAT files."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import iterparse

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None

ARCADE_ROM_EXTENSIONS: frozenset[str] = frozenset({"zip", "7z"})


@dataclass
class DatInfo:
    label: str          # display name derived from filename
    path: Path
    all_machines: set[str] = field(default_factory=set)     # all names including clones
    parent_machines: set[str] = field(default_factory=set)  # cloneof is None
    warning: str | None = None


@dataclass
class DatMatchResult:
    dat: DatInfo
    # folder stems that are in this DAT
    matched_all: set[str] = field(default_factory=set)
    matched_parents: set[str] = field(default_factory=set)


@dataclass
class FolderDatCheckResult:
    folder: Path
    folder_stems: set[str] = field(default_factory=set)
    dats: list[DatInfo] = field(default_factory=list)
    results: list[DatMatchResult] = field(default_factory=list)
    # stems present in folder but not in ANY dat
    unmatched_in_any: set[str] = field(default_factory=set)


def _open_xml_source(dat_path: Path):
    """Yield an open binary file-like object for the XML inside dat_path."""
    if dat_path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(dat_path)
        xml_entries = [n for n in zf.namelist() if n.lower().endswith((".xml", ".dat"))]
        if not xml_entries:
            zf.close()
            raise ValueError(f"No XML/DAT entry found inside {dat_path.name}")
        return zf, zf.open(xml_entries[0])
    else:
        fh = open(dat_path, "rb")
        return None, fh


def _parse_dat(dat_path: Path) -> DatInfo:
    """Parse a MAME XML DAT file (or ZIP containing one) and return DatInfo."""
    label = dat_path.stem
    # Strip common suffixes to make a cleaner label
    for suffix in (" XML", " Arcade XML", " Home XML", "-xml", "_xml"):
        if label.lower().endswith(suffix.lower()):
            label = label[: -len(suffix)]

    info = DatInfo(label=label, path=dat_path)
    zf, fh = _open_xml_source(dat_path)
    try:
        seen_names: set[str] = set()
        for event, elem in iterparse(fh, events=["end"]):
            if elem.tag not in ("machine", "game"):
                continue
            name = elem.get("name", "")
            cloneof = elem.get("cloneof")
            elem.clear()
            if not name:
                continue
            name_lc = name.lower()
            if name_lc in seen_names:
                continue
            seen_names.add(name_lc)
            info.all_machines.add(name_lc)
            if cloneof is None:
                info.parent_machines.add(name_lc)
    finally:
        fh.close()
        if zf:
            zf.close()

    return info


def _detect_duplicate_dats(dats: list[DatInfo]) -> None:
    """Mark DATs that appear to have identical machine sets."""
    for i, a in enumerate(dats):
        for b in dats[i + 1:]:
            if a.all_machines == b.all_machines:
                b.warning = f"identical machine list to '{a.label}' — possibly mislabelled"


def _index_folder(folder: Path) -> set[str]:
    """Return lowercase stems of all ROM zip/7z files in folder (non-recursive)."""
    stems: set[str] = set()
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower().lstrip(".") in ARCADE_ROM_EXTENSIONS:
            stems.add(p.stem.lower())
    return stems


def run_dat_check(
    folder: Path,
    dat_paths: list[Path],
    *,
    detail: bool = False,
    parents_only: bool = False,
) -> FolderDatCheckResult:
    """Compare folder contents against each DAT and print a coverage report."""
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    console = Console() if Console else None

    _print(console, f"Indexing folder: [bold]{folder}[/bold]" if console else f"Indexing folder: {folder}")
    folder_stems = _index_folder(folder)
    _print(console, f"Found {len(folder_stems)} ROM files\n")

    dats: list[DatInfo] = []
    for dp in dat_paths:
        _print(console, f"Parsing DAT: {dp.name}")
        try:
            info = _parse_dat(dp)
            dats.append(info)
        except Exception as exc:
            _print(console, f"  [red]Error:[/red] {exc}" if console else f"  Error: {exc}")

    _detect_duplicate_dats(dats)

    results: list[DatMatchResult] = []
    all_matched: set[str] = set()
    for dat in dats:
        r = DatMatchResult(dat=dat)
        machine_set = dat.parent_machines if parents_only else dat.all_machines
        r.matched_all = folder_stems & dat.all_machines
        r.matched_parents = folder_stems & dat.parent_machines
        all_matched |= r.matched_all
        results.append(r)

    result = FolderDatCheckResult(
        folder=folder,
        folder_stems=folder_stems,
        dats=dats,
        results=results,
        unmatched_in_any=folder_stems - all_matched,
    )

    _print_summary(result, detail=detail, console=console)
    return result


def _pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "—"
    return f"{100 * numerator // denominator}%"


def _print(console, text: str) -> None:
    if console:
        console.print(text)
    else:
        print(text)


def _print_summary(result: FolderDatCheckResult, *, detail: bool, console) -> None:
    folder_count = len(result.folder_stems)

    # Best match = DAT that recognises the highest % of the folder's files
    best: DatMatchResult | None = max(
        result.results, key=lambda r: len(r.matched_all), default=None
    )

    if console and Table:
        table = Table(show_lines=False, header_style="bold cyan")
        table.add_column("DAT", style="bold")
        table.add_column("Machines (total)", justify="right")
        table.add_column("Parents only", justify="right")
        table.add_column("Matched in folder", justify="right")
        table.add_column("Folder match", justify="right", style="green")
        table.add_column("Collection %", justify="right")
        table.add_column("Notes")

        for r in result.results:
            dat = r.dat
            is_best = best and r is best and len(result.results) > 1
            notes = dat.warning or ("[bold green]← best match[/bold green]" if is_best else "")
            table.add_row(
                dat.label,
                str(len(dat.all_machines)),
                str(len(dat.parent_machines)),
                str(len(r.matched_all)),
                _pct(len(r.matched_all), folder_count),
                _pct(len(r.matched_parents), len(dat.parent_machines)),
                notes,
            )

        console.print(f"\nFolder: [bold]{result.folder}[/bold]  ({folder_count} files)\n")
        console.print(table)

        if best:
            match_pct = 100 * len(best.matched_all) // folder_count if folder_count else 0
            console.print(
                f"\n[bold]Best match:[/bold] [green]{best.dat.label}[/green] "
                f"— {match_pct}% of folder files recognised "
                f"({len(best.matched_all)} / {folder_count})"
            )
        console.print(
            f"[bold]Unmatched in any DAT:[/bold] {len(result.unmatched_in_any)} files "
            f"({_pct(len(result.unmatched_in_any), folder_count)} of folder)"
        )
    else:
        print(f"\nFolder: {result.folder}  ({folder_count} files)\n")
        col_w = max((len(r.dat.label) for r in result.results), default=10) + 2
        header = (
            f"{'DAT':<{col_w}} {'Total':>8} {'Parents':>8} "
            f"{'Matched':>8} {'Folder match':>13} {'Collection %':>13}  Notes"
        )
        print(header)
        print("-" * len(header))
        for r in result.results:
            dat = r.dat
            is_best = best and r is best and len(result.results) > 1
            note = f"  ← {dat.warning}" if dat.warning else ("  ← best match" if is_best else "")
            print(
                f"{dat.label:<{col_w}} {len(dat.all_machines):>8} {len(dat.parent_machines):>8} "
                f"{len(r.matched_all):>8} {_pct(len(r.matched_all), folder_count):>13} "
                f"{_pct(len(r.matched_parents), len(dat.parent_machines)):>13}{note}"
            )

        if best:
            match_pct = 100 * len(best.matched_all) // folder_count if folder_count else 0
            print(f"\nBest match: {best.dat.label} — {match_pct}% of folder files recognised ({len(best.matched_all)} / {folder_count})")
        print(
            f"Unmatched in any DAT: {len(result.unmatched_in_any)} files "
            f"({_pct(len(result.unmatched_in_any), folder_count)} of folder)"
        )

    if detail and result.unmatched_in_any:
        _print(console, "\nFiles in folder not found in any DAT:")
        for name in sorted(result.unmatched_in_any):
            _print(console, f"  {name}.zip")
