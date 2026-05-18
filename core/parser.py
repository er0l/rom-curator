"""Filename parsing helpers for ROM metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


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
# Standard No-Intro/Redump standalone disc tag: (Disc 1), (Disk 2), (Side A), …
DISC_RE = re.compile(r"\((?:Disc|Disk|Side|Tape|Part)\s*(?:\d+|[A-Z])\)", re.IGNORECASE)
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
    stem = Path(filename).stem
    tags = [first or second for first, second in PAREN_TAG_RE.findall(stem)]

    title = PAREN_TAG_RE.sub("", stem)
    title = re.sub(r"\s+", " ", title).strip(" -_")
    if not title:
        title = stem

    region = _detect_region(tags)
    revision = _detect_revision(tags)
    disc = _detect_disc(Path(filename).stem)
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


def _detect_disc(stem: str) -> str | None:
    # Prefer the canonical standalone form first: (Disc 1)
    match = DISC_RE.search(stem)
    if match:
        return match.group(0)
    # Region-before-disc combined form: (NA - Disc 1) → "(Disc 1)"
    match = DISC_COMBINED_RE.search(stem)
    if match:
        return f"({match.group(1)})"
    # Disc-before-region/subtitle form: (Disc 1 - EU) → "(Disc 1)"
    match = DISC_PREFIX_RE.search(stem)
    if match:
        return f"({match.group(1)})"
    return None
