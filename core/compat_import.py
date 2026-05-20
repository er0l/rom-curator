"""Import compatibility lists from xlsx spreadsheets into compat YAML files.

Parses xlsx files produced by the R36S / RK3326 community compatibility
testing project (https://github.com/GazousGit/R36S-Game-Compatibility-Lists)
and writes normalised YAML compat lists under mappings/compat/{chip}/.

Supported column layouts are auto-detected from the header row:
  - If a "Rom" / "File" column with .zip filenames is present → match_by=stem
  - Otherwise → match_by=title (normalised game name)
"""

from __future__ import annotations

import re
from pathlib import Path

from .compat import CompatList, PLAYABILITY_RANK, normalise_key, save_compat_list

try:
    import openpyxl as _openpyxl
except ImportError:  # pragma: no cover
    _openpyxl = None  # type: ignore[assignment]


# Map keywords found in xlsx filenames to canonical system names.
# Order matters: more specific patterns first.
_FILENAME_SYSTEM_MAP: list[tuple[str, str]] = [
    ("atomiswave",       "atomiswave"),
    ("naomi 2",          "naomi2"),
    ("naomi2",           "naomi2"),
    ("naomi",            "naomi"),
    ("dreamcast",        "dreamcast"),
    ("saturn",           "saturn"),
    ("nintendo 64",      "n64"),
    (" n64 ",            "n64"),
    ("playstation 2",    "ps2"),
    (" ps2 ",            "ps2"),
    ("ppsspp",           "psp"),
    (" psp ",            "psp"),
    ("playstation",      "psx"),
    (" psx ",            "psx"),
    ("nds drastic",      "nds"),
    (" nds ",            "nds"),
    ("nintendo ds",      "nds"),
    ("gamecube",         "gamecube"),
    ("wii u",            "wiiu"),
    (" wii ",            "wii"),
    ("game boy advance", "gba"),
    (" gba ",            "gba"),
    ("snes",             "snes"),
    ("mega drive",       "megadrive"),
    ("genesis",          "megadrive"),
    ("neo geo",          "neogeo"),
    ("neogeo",           "neogeo"),
    ("3ds",              "3ds"),
    ("switch",           "switch"),
    ("xbox",             "xbox"),
]


def detect_system_from_filename(filename: str) -> str | None:
    """Try to infer canonical system name from the xlsx filename."""
    lower = filename.lower()
    for keyword, system in _FILENAME_SYSTEM_MAP:
        if keyword in lower:
            return system
    return None


def _find_column(headers: list[str | None], *names: str) -> int | None:
    """Return 0-based index of first header matching any of *names* (case-insensitive).

    Tries exact match first, then startswith, to handle headers with extra
    annotations like 'Game Name (Color = Result from old list)'.
    """
    lowered = [str(h).lower().strip() if h is not None else "" for h in headers]
    # Exact match
    for name in names:
        try:
            return lowered.index(name.lower())
        except ValueError:
            pass
    # Startswith match
    for name in names:
        needle = name.lower()
        for i, h in enumerate(lowered):
            if h.startswith(needle):
                return i
    return None


def _extract_stem(cell_value: str) -> str | None:
    """Extract lowercase stem from a ROM cell.

    Handles:
      - Filenames with extension: 'anmlbskt.zip' → 'anmlbskt'
      - Bare MAME machine names:  '18wheelr'      → '18wheelr'
    """
    v = str(cell_value).strip()
    if not v or v.lower() in ("none", "bin file", "n/a", "-", ""):
        return None
    stem = Path(v).stem.lower()
    # Reject stems that look like generic notes (contain spaces → not a ROM name)
    if " " in stem:
        return None
    return stem or None


def parse_xlsx(
    xlsx_path: str | Path,
    system: str,
    chip: str,
) -> CompatList:
    """Parse an xlsx compatibility list and return a CompatList.

    Raises ImportError if openpyxl is not installed.
    Raises ValueError if required columns cannot be found.
    """
    if _openpyxl is None:
        raise ImportError(
            "openpyxl is required for compat-import. "
            "Install it with: pip install openpyxl"
        )

    xlsx_path = Path(xlsx_path)
    wb = _openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)

    # Use the first sheet (usually "Games")
    ws = wb.worksheets[0]
    rows_iter = ws.iter_rows(values_only=True)

    # Read header row
    header = list(next(rows_iter, []))

    # Locate columns
    name_col = _find_column(header, "game name", "name", "title", "game")
    play_col = _find_column(header, "playability", "compatibility", "performance", "rating")
    rom_col  = _find_column(header, "rom", "file", "filename", "rom file")

    if name_col is None:
        raise ValueError(f"Cannot find 'Game Name' column in {xlsx_path.name}. Headers: {header}")
    if play_col is None:
        raise ValueError(f"Cannot find 'Playability' column in {xlsx_path.name}. Headers: {header}")

    # Decide match strategy:
    # If the ROM column exists AND its first non-header data cell looks like a
    # filename (contains a dot and recognisable extension), use stem matching.
    match_by = "title"
    if rom_col is not None:
        # Peek at first few data rows to check if ROM column has filenames
        sample_rows = []
        for row in rows_iter:
            sample_rows.append(row)
            if len(sample_rows) >= 5:
                break
        def _looks_like_rom_id(v) -> bool:
            if v is None:
                return False
            s = str(v).strip()
            if s.lower() in ("none", "bin file", "n/a", "-", ""):
                return False
            # Filename with extension (anmlbskt.zip) OR bare MAME stem (18wheelr)
            if re.search(r"\.\w{2,5}$", s):
                return True
            # Bare alphanumeric stem: no spaces, 2-16 chars, no dots
            if re.fullmatch(r"[a-zA-Z0-9_\-]{2,16}", s):
                return True
            return False

        has_filenames = any(
            _looks_like_rom_id(r[rom_col])
            for r in sample_rows
            if len(r) > rom_col
        )
        if has_filenames:
            match_by = "stem"
        # Reset iterator-like access using the already-read sample
        remaining_rows = list(sample_rows)
        for row in rows_iter:
            remaining_rows.append(row)
    else:
        remaining_rows = list(rows_iter)

    # Build the games dict
    games: dict[str, str] = {}
    for row in remaining_rows:
        if not row or all(v is None for v in row):
            continue

        name_val = row[name_col] if name_col < len(row) else None
        play_val = row[play_col] if play_col < len(row) else None

        if name_val is None or play_val is None:
            continue

        playability = str(play_val).strip()
        if playability not in PLAYABILITY_RANK:
            continue  # skip rows with unexpected values (notes, blanks, etc.)

        if match_by == "stem":
            rom_val = row[rom_col] if rom_col is not None and rom_col < len(row) else None
            key = _extract_stem(str(rom_val)) if rom_val is not None else None
            if not key:
                # Fall back to normalised title for entries without a ROM file
                key = normalise_key(str(name_val))
        else:
            key = normalise_key(str(name_val))

        if not key:
            continue

        games[key] = playability

    wb.close()

    return CompatList(
        system=system,
        chip=chip,
        match_by=match_by,
        games=games,
    )


def run_compat_import(
    xlsx_paths: list[str | Path],
    chip: str,
    mappings_dir: Path,
    system_overrides: dict[str, str] | None = None,
) -> list[tuple[Path, CompatList]]:
    """Parse each xlsx file and save to mappings/compat/{chip}/{system}.yaml.

    Returns a list of (output_path, CompatList) tuples for reporting.
    System is auto-detected from the filename unless overridden in
    system_overrides ({xlsx_stem: system_name}).
    """
    results = []
    for xlsx_path in xlsx_paths:
        xlsx_path = Path(xlsx_path)
        stem = xlsx_path.stem

        system = (system_overrides or {}).get(stem)
        if system is None:
            system = detect_system_from_filename(xlsx_path.name)
        if system is None:
            print(f"  Warning: cannot detect system for '{xlsx_path.name}' — use --system to specify")
            continue

        print(f"  Parsing: {xlsx_path.name}  →  {system}")
        compat = parse_xlsx(xlsx_path, system=system, chip=chip)

        output_path = mappings_dir / "compat" / chip / f"{system}.yaml"
        save_compat_list(compat, output_path)

        total = len(compat.games)
        from collections import Counter
        counts = Counter(compat.games.values())
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -PLAYABILITY_RANK.get(x[0], -1)))
        print(f"    {total} entries ({breakdown})  →  {output_path}")
        results.append((output_path, compat))

    return results
