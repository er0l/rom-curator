"""Filename parsing helpers for ROM metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


# Compound format pseudo-extensions that precede the real extension.
# e.g. "Game.xiso.iso" → strip ".xiso" before parsing so it doesn't
# pollute the title.
_COMPOUND_EXTS = re.compile(
    r"\.(xiso|nkit|gcm|rvz|wbfs)$",
    re.IGNORECASE,
)

REGION_ALIASES = {
    "USA": "USA",
    "U": "USA",
    "US": "USA",
    "NA": "USA",   # North America — common in disc-based collections
    "EUROPE": "Europe",
    "EUR": "Europe",
    "EU": "Europe",
    "E": "Europe",
    "PAL": "Europe",
    "JAPAN": "Japan",
    "JPN": "Japan",
    "JP": "Japan",
    "J": "Japan",
    "WORLD": "World",
    "W": "World",
}

REGION_SPLIT_RE = re.compile(r"[,/&+]|(?:\s+-\s+)|\s+")
PAREN_TAG_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
REVISION_RE = re.compile(r"\b(?:Rev(?:ision)?\.?\s*[A-Za-z0-9]+|v\d+(?:\.\d+)*)\b", re.IGNORECASE)

# Standard paren disc tag — extended to handle:
#   (Disc 1)              No-Intro / Redump canonical form
#   (Disk 1 of 4)         WHDLoad / MSX "of N" total-count form
#   (Data Disk 1 of 4)    MSXturboR labelled disks ("Data Disk", "Game Disk", …)
#   (Side A), (Tape 2)    cassette / flipside variants
# One optional prefix word (Data, Game, Opening, …) before Disk/Disc is allowed.
DISC_RE = re.compile(
    r"\((?:(?:\w+\s+)?(?:Disc|Disk)|Side|Tape|Part)\s*(?:\d+|[A-Z])(?:\s+of\s+\d+)?\)",
    re.IGNORECASE,
)
# Combined region+disc tag used by some collections: (NA - Disc 1), (JP - Disc 2 - subtitle)
# Captures just the "Disc N" portion so it can be normalised to "(Disc N)".
DISC_COMBINED_RE = re.compile(
    r"\([^()]*?-\s*((?:Disc|Disk|Side|Tape|Part)\s*(?:\d+|[A-Z]))\b",
    re.IGNORECASE,
)
# Disc-first combined tag: (Disc 1 - EU), (Disc 2 - English Patch)
# Captures the "Disc N" portion before the trailing " - region/subtitle".
DISC_PREFIX_RE = re.compile(
    r"\(((?:Disc|Disk|Side|Tape|Part)\s*(?:\d+|[A-Z]))\s*-",
    re.IGNORECASE,
)
# Suffix-style disc tags NOT inside parentheses — common in Amiga WHDLoad and C64 sets.
#   _Disk1  _DiskA  -Disk2  -disk-A   (underscore or hyphen separator)
# The identifier (number or single letter) must be followed by end-of-string,
# another separator, or an opening bracket — not a plain letter (avoids "Discs").
DISC_SUFFIX_RE = re.compile(
    r"[_\-](?:Disc|Disk)[_\-]?(\d+|[A-Za-z])(?=[_\-\[\(]|$)",
    re.IGNORECASE,
)
# Space-separated suffix with digit only: " disk1", " Disk2"
# Digit-only avoids false positives on words like "discs" or "diskette".
DISC_SUFFIX_SPACE_RE = re.compile(
    r"\s(?:Disc|Disk)(\d+)(?=[_\-\[\(]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedRomName:
    title: str
    region: str | None
    revision: str | None
    disc: str | None
    is_beta: bool
    is_proto: bool
    is_demo: bool
    is_translation: bool
    is_hack: bool


def parse_filename(filename: str) -> ParsedRomName:
    """Parse common No-Intro/Redump-style ROM filename metadata."""
    stem = _COMPOUND_EXTS.sub("", Path(filename).stem)
    tags = [first or second for first, second in PAREN_TAG_RE.findall(stem)]

    title = PAREN_TAG_RE.sub("", stem)
    title = re.sub(r"\s+", " ", title).strip(" -_")
    if not title:
        title = stem

    disc = _detect_disc(stem)

    # Suffix-style disc tags (_Disk1, -DiskA, " disk1") are not inside
    # parentheses so PAREN_TAG_RE leaves them in the title string.
    # Strip everything from the disc marker onwards when one is found.
    if disc:
        suffix_m = DISC_SUFFIX_RE.search(title) or DISC_SUFFIX_SPACE_RE.search(title)
        if suffix_m:
            stripped = title[:suffix_m.start()].strip(" -_")
            if stripped:
                title = stripped

    region = _detect_region(tags)
    revision = _detect_revision(tags)
    lowered_tags = " ".join(tags).lower()

    return ParsedRomName(
        title=title,
        region=region,
        revision=revision,
        disc=disc,
        is_beta="beta" in lowered_tags,
        is_proto="proto" in lowered_tags or "prototype" in lowered_tags,
        is_demo="demo" in lowered_tags,
        is_translation="translation" in lowered_tags or "translated" in lowered_tags,
        is_hack="hack" in lowered_tags or "romhack" in lowered_tags,
    )


def parse_rom_name(filename: str) -> ParsedRomName:
    """Backward-compatible alias for older checklist/test snippets."""
    return parse_filename(filename)


def _detect_region(tags: list[str]) -> str | None:
    for tag in tags:
        normalized_tag = tag.strip().upper()
        if normalized_tag in REGION_ALIASES:
            return REGION_ALIASES[normalized_tag]

        parts = [part.strip().upper() for part in REGION_SPLIT_RE.split(tag) if part.strip()]
        for part in parts:
            if part in REGION_ALIASES:
                return REGION_ALIASES[part]
    return None


def _detect_revision(tags: list[str]) -> str | None:
    for tag in tags:
        match = REVISION_RE.search(tag)
        if match:
            revision = match.group(0).strip()
            return re.sub(r"^revision\b", "Rev", revision, flags=re.IGNORECASE)
    return None


def _normalise_disc(raw: str) -> str:
    """Normalise a raw disc tag to a clean '(Keyword N)' form.

    Strips 'of M' totals and prefix words so that:
      '(Disk 1 of 4)'      → '(Disk 1)'
      '(Data Disk 2 of 6)' → '(Disk 2)'
      '(Disc 1)'           → '(Disc 1)'   (unchanged)
      '(Side A)'           → '(Side A)'   (unchanged)
    """
    m = re.search(r"(disc|disk|side|tape|part)\s*(\d+|[A-Za-z])", raw, re.IGNORECASE)
    if not m:
        return raw
    return f"({m.group(1).capitalize()} {m.group(2)})"


def _detect_disc(stem: str) -> str | None:
    # 1. Paren forms: (Disc 1), (Disk 1 of 4), (Data Disk 2 of 6), (Side A), …
    match = DISC_RE.search(stem)
    if match:
        return _normalise_disc(match.group(0))
    # 2. Region-before-disc combined form: (NA - Disc 1) → "(Disc 1)"
    match = DISC_COMBINED_RE.search(stem)
    if match:
        return _normalise_disc(f"({match.group(1)})")
    # 3. Disc-before-region/subtitle form: (Disc 1 - EU) → "(Disc 1)"
    match = DISC_PREFIX_RE.search(stem)
    if match:
        return _normalise_disc(f"({match.group(1)})")
    # 4. Suffix forms not in parens: _Disk1  _DiskA  -Disk2  -disk-A
    match = DISC_SUFFIX_RE.search(stem)
    if match:
        ident = match.group(1)
        return f"(Disc {ident.upper() if ident.isalpha() else ident})"
    # 5. Space-separated suffix with digit: " disk1"  " Disk2"
    match = DISC_SUFFIX_SPACE_RE.search(stem)
    if match:
        return f"(Disc {match.group(1)})"
    return None
