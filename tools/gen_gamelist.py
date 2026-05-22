"""Generate gamelist.xml for EmulationStation-compatible frontends.

For each system, queries the ROM inventory database, resolves media assets
from the system's subfolders, and writes (or merges into) a gamelist.xml.

Two media naming conventions are supported automatically:

  Scraper-suffix style (Batocera / Skyscraper):
    images/{title}-image.png          → <image>
    images/{title}-thumb.png          → <thumbnail>
    images/{title}-marquee.png        → <marquee>
    videos/{title}-video.mp4          → <video>

  Full-stem style (ScreenScraper / RetroPie):
    boxart/{filename_stem}.png        → <image>
    wheel/{filename_stem}.png         → <marquee>
    snap/{filename_stem}.mp4          → <video>
    screenshots/{filename_stem}.png   → <screenshot>
    cartart/{filename_stem}.png       → (no standard gamelist field — skipped)
    fanarts/{filename_stem}.png       → <fanart>  (ES-DE extension)
    backcovers/{filename_stem}.png    → (skipped)

Merging behaviour:
  If gamelist.xml already exists it is parsed first.  Fields that cannot be
  auto-generated (desc, playcount, lastplayed, favorite, hidden) are
  preserved from the existing file.  Media paths and metadata are refreshed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from core.database import InventoryDatabase


# ---------------------------------------------------------------------------
# Media layout
# ---------------------------------------------------------------------------

_IMAGE_EXTS = (".png", ".jpg", ".jpeg")
_VIDEO_EXTS = (".mp4", ".avi", ".mkv")

# Each entry: (gamelist_field, [(subfolder, name_template, extensions)])
# name_template uses {title} or {stem} as placeholder.
# Priority: first match wins.
_MEDIA_FIELDS: list[tuple[str, list[tuple[str, str, tuple[str, ...]]]]] = [
    ("image", [
        ("images",      "{title}-image",    _IMAGE_EXTS),   # Batocera/Skyscraper suffix
        ("images",      "{stem}",           _IMAGE_EXTS),   # plain stem in images/
        ("boxart",      "{stem}",           _IMAGE_EXTS),
        ("mixart",      "{stem}",           _IMAGE_EXTS),
    ]),
    ("thumbnail", [
        ("images",      "{title}-thumb",    _IMAGE_EXTS),
    ]),
    ("marquee", [
        ("images",      "{title}-marquee",  _IMAGE_EXTS),   # Batocera/Skyscraper suffix
        ("wheel",       "{stem}",           _IMAGE_EXTS),
        ("marquee",     "{stem}",           _IMAGE_EXTS),
        ("logos",       "{stem}",           _IMAGE_EXTS),
    ]),
    ("video", [
        ("videos",      "{title}-video",    _VIDEO_EXTS),   # Batocera/Skyscraper suffix
        ("videos",      "{stem}",           _VIDEO_EXTS),   # plain stem in videos/
        ("snap",        "{stem}",           _VIDEO_EXTS),
    ]),
    ("screenshot", [
        ("screenshots", "{stem}",           _IMAGE_EXTS),
    ]),
    ("fanart", [
        ("fanarts",     "{stem}",           _IMAGE_EXTS),
        ("flyer",       "{stem}",           _IMAGE_EXTS),
    ]),
]

# gamelist.xml fields we never regenerate — preserved from existing file.
_PRESERVE_FIELDS = frozenset({
    "desc", "playcount", "lastplayed", "favorite", "hidden", "kidgame",
    "lang", "region",
})


# ---------------------------------------------------------------------------
# Media resolution
# ---------------------------------------------------------------------------

def _find_media(system_dir: Path, stem: str, title: str, field: str) -> str | None:
    """Return a relative path string (from system_dir) for *field*, or None."""
    for fld, candidates in _MEDIA_FIELDS:
        if fld != field:
            continue
        for subfolder, template, exts in candidates:
            base = template.replace("{title}", title).replace("{stem}", stem)
            for ext in exts:
                path = system_dir / subfolder / (base + ext)
                if path.exists():
                    return f"./{subfolder}/{base}{ext}"
    return None


# ---------------------------------------------------------------------------
# Existing gamelist.xml parsing
# ---------------------------------------------------------------------------

def _parse_existing(gamelist_path: Path) -> dict[str, dict[str, str]]:
    """Return {path_value: {field: text}} from an existing gamelist.xml."""
    if not gamelist_path.exists():
        return {}
    try:
        tree = ET.parse(gamelist_path)
    except ET.ParseError:
        return {}
    result: dict[str, dict[str, str]] = {}
    for game in tree.getroot().findall("game"):
        path_el = game.find("path")
        if path_el is None or not path_el.text:
            continue
        fields: dict[str, str] = {}
        for child in game:
            if child.tag != "path" and child.text:
                fields[child.tag] = child.text
        result[path_el.text] = fields
    return result


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _rating_str(value) -> str | None:
    """Convert 0–100 ROMM/IGDB score to '0.00'–'1.00' gamelist rating."""
    try:
        f = float(value)
        return f"{f / 100:.2f}"
    except (TypeError, ValueError):
        return None


def _releasedate_str(year) -> str | None:
    try:
        return f"{int(year)}0101T000000"
    except (TypeError, ValueError):
        return None


def _first(semicolon_str: str | None) -> str | None:
    if not semicolon_str:
        return None
    return semicolon_str.split(";")[0].strip() or None


def _players_str(value) -> str | None:
    """Convert ROMM player_count to a gamelist players string.

    Handles integers, range strings like '1-2', and nulls.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "0", ""):
        return None
    # Already a range string (e.g. '1-2') — use as-is.
    if "-" in s:
        return s
    # Try integer
    try:
        n = int(s)
        return str(n) if n > 0 else None
    except ValueError:
        return s or None


# ---------------------------------------------------------------------------
# XML building
# ---------------------------------------------------------------------------

def _add_text(parent: ET.Element, tag: str, text: str | None) -> None:
    if text:
        el = ET.SubElement(parent, tag)
        el.text = text


def _build_game_element(
    path_value: str,
    name: str,
    media: dict[str, str | None],
    metadata: dict[str, str | None],
    preserved: dict[str, str],
) -> ET.Element:
    game = ET.Element("game")

    ET.SubElement(game, "path").text = path_value
    ET.SubElement(game, "name").text = name

    # desc: from metadata (ROMM summary or preserved from existing gamelist)
    _add_text(game, "desc", metadata.get("desc") or preserved.get("desc"))

    # Media fields
    for field in ("image", "thumbnail", "marquee", "video", "screenshot", "fanart"):
        _add_text(game, field, media.get(field))

    # Metadata fields
    _add_text(game, "rating",      metadata.get("rating"))
    _add_text(game, "releasedate", metadata.get("releasedate"))
    _add_text(game, "developer",   metadata.get("developer"))
    _add_text(game, "publisher",   metadata.get("publisher"))
    _add_text(game, "genre",       metadata.get("genre"))
    _add_text(game, "players",     metadata.get("players"))

    # Other preserved fields
    for field in ("playcount", "lastplayed", "favorite", "hidden", "kidgame"):
        val = preserved.get(field)
        if val:
            _add_text(game, field, val)

    return game


def _pretty_xml(root: ET.Element) -> str:
    raw = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(raw)
    lines = reparsed.toprettyxml(indent="\t").splitlines()
    # minidom adds an XML declaration line; keep it clean
    result = []
    for line in lines:
        stripped = line.rstrip()
        if stripped == '<?xml version="1.0" ?>':
            result.append('<?xml version="1.0"?>')
        elif stripped:
            result.append(stripped)
    return "\n".join(result) + "\n"


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------

def generate_gamelist(
    system: str,
    roms_root: Path,
    database_path: Path,
    nas_folder: str | None = None,
    *,
    media_dir: Path | None = None,
    gamelist_dir: Path | None = None,
    rom_path_prefix: str = "",
    dry_run: bool = False,
) -> dict[str, int]:
    """Generate or update gamelist.xml for *system*.

    Returns a stats dict with keys: total, with_image, with_video,
    with_metadata, skipped_missing, written.

    For subpath systems (e.g. nas_folder='arcade/mame2003-plus'):
    - *media_dir* is the parent folder where images/ and videos/ live
    - *gamelist_dir* is where gamelist.xml is written (same parent)
    - *rom_path_prefix* is prepended to filenames in <path> (e.g. 'mame2003-plus/')
    """
    stats: dict[str, int] = {
        "total": 0, "with_image": 0, "with_video": 0,
        "with_metadata": 0, "skipped_missing": 0, "written": 0,
    }

    # Determine the NAS system folder (where ROM files live).
    folder_name = nas_folder or system
    system_dir = roms_root / folder_name
    if not system_dir.is_dir():
        raise FileNotFoundError(f"System folder not found: {system_dir}")

    # Media and gamelist may live in a parent folder for subpath systems.
    _media_dir    = media_dir    or system_dir
    _gamelist_dir = gamelist_dir or system_dir

    gamelist_path = _gamelist_dir / "gamelist.xml"
    existing = _parse_existing(gamelist_path)

    with InventoryDatabase(database_path) as db:
        db.initialize()

        # Fetch all ROMs for this system with ROMM and MAME metadata.
        rows = db.fetch_all(
            """
            SELECT
                r.filename,
                r.title,
                r.relative_path,
                rr.name          AS romm_name,
                rr.total_rating,
                rr.year          AS romm_year,
                rr.genres,
                rr.player_count,
                rr.summary       AS romm_summary,
                rr.developer     AS romm_developer,
                rr.publisher     AS romm_publisher,
                mm.description   AS mame_desc,
                mm.manufacturer  AS mame_manufacturer,
                mm.year          AS mame_year
            FROM roms r
            LEFT JOIN romm_roms rr
                ON rr.canonical_system = r.system
                AND (rr.fs_name = r.filename
                     OR rr.fs_stem = CASE
                         WHEN SUBSTR(r.filename,-5,1)='.' THEN SUBSTR(r.filename,1,LENGTH(r.filename)-5)
                         WHEN SUBSTR(r.filename,-4,1)='.' THEN SUBSTR(r.filename,1,LENGTH(r.filename)-4)
                         WHEN SUBSTR(r.filename,-3,1)='.' THEN SUBSTR(r.filename,1,LENGTH(r.filename)-3)
                         ELSE r.filename END)
            LEFT JOIN mame_machines mm
                ON mm.name = r.title AND r.system IN ('arcade','mame2003-plus')
            WHERE r.system = ?
            ORDER BY r.title, r.filename
            """,
            (system,),
        )

    root = ET.Element("gameList")

    for row in rows:
        stats["total"] += 1
        filename   = str(row["filename"])
        title      = str(row["title"])
        stem       = Path(filename).stem
        path_value = f"./{rom_path_prefix}{filename}"

        # Verify the ROM file exists.
        rom_path = system_dir / filename
        if not rom_path.exists():
            stats["skipped_missing"] += 1
            continue

        # Resolve media from the media root (parent folder for subpath systems).
        media: dict[str, str | None] = {}
        for field, _ in _MEDIA_FIELDS:
            media[field] = _find_media(_media_dir, stem, title, field)
        if media.get("image"):
            stats["with_image"] += 1
        if media.get("video"):
            stats["with_video"] += 1

        # Build metadata from ROMM / MAME.
        romm_name      = row["romm_name"]
        rating         = _rating_str(row["total_rating"])
        year           = row["romm_year"] or row["mame_year"]
        genres         = str(row["genres"]) if row["genres"] else None
        player_ct      = row["player_count"]
        mame_mfr       = row["mame_manufacturer"]
        romm_summary   = str(row["romm_summary"]) if row["romm_summary"] else None
        romm_developer = str(row["romm_developer"]) if row["romm_developer"] else None
        romm_publisher = str(row["romm_publisher"]) if row["romm_publisher"] else None

        preserved = existing.get(path_value, {})
        metadata: dict[str, str | None] = {
            "rating":      rating,
            "releasedate": _releasedate_str(year),
            "genre":       _first(genres),
            "players":     _players_str(player_ct),
            # developer: ROMM > MAME manufacturer > preserved from existing gamelist
            "developer":   romm_developer or (str(mame_mfr) if mame_mfr else preserved.get("developer")),
            # publisher: ROMM > preserved from existing gamelist
            "publisher":   romm_publisher or preserved.get("publisher"),
            # description: ROMM summary > preserved from existing gamelist
            "desc":        romm_summary or preserved.get("desc"),
        }
        if any(v for v in metadata.values()):
            stats["with_metadata"] += 1

        # Display name: ROMM name > parsed title
        display_name = str(romm_name) if romm_name else title

        game_el = _build_game_element(path_value, display_name, media, metadata, preserved)
        root.append(game_el)

    stats["written"] = len(root)

    if dry_run:
        return stats

    xml_text = _pretty_xml(root)
    gamelist_path.write_text(xml_text, encoding="utf-8")
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_gen_gamelist(
    config: dict[str, object],
    systems: list[str],
    mappings: dict[str, dict[str, object]],
    *,
    dry_run: bool = False,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root     = Path(str(paths["roms"])).expanduser()
    database_path = Path(str(paths["database"])).expanduser()

    if not database_path.exists():
        raise FileNotFoundError(f"Inventory database not found: {database_path}")

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
    except ImportError:
        console = None  # type: ignore[assignment]

    rows_out = []
    for system in systems:
        nas_folder = None
        sys_meta = mappings.get(system, {})
        if isinstance(sys_meta, dict) and sys_meta.get("nas"):
            nas_folder = str(sys_meta["nas"])

        folder_name = nas_folder or system
        system_dir  = roms_root / folder_name
        if not system_dir.is_dir():
            rows_out.append((system, folder_name, "—", "—", "—", "—", "folder not found"))
            continue

        # For subpath systems (e.g. arcade/mame2003-plus), media and gamelist.xml
        # live in the parent folder (arcade/) per Batocera convention.
        # ROM <path> entries are prefixed with the subfolder name.
        if nas_folder and "/" in nas_folder:
            parent_nas     = nas_folder.rsplit("/", 1)[0]
            subfolder_name = nas_folder.rsplit("/", 1)[1]
            media_dir      = roms_root / parent_nas
            gamelist_dir   = roms_root / parent_nas
            rom_path_prefix = f"{subfolder_name}/"
        else:
            media_dir       = system_dir
            gamelist_dir    = system_dir
            rom_path_prefix = ""

        try:
            stats = generate_gamelist(
                system, roms_root, database_path,
                nas_folder=folder_name,
                media_dir=media_dir,
                gamelist_dir=gamelist_dir,
                rom_path_prefix=rom_path_prefix,
                dry_run=dry_run,
            )
            gamelist = gamelist_dir / "gamelist.xml"
            status = "DRY RUN" if dry_run else f"written → {gamelist}"
            rows_out.append((
                system,
                folder_name,
                str(stats["written"]),
                str(stats["with_image"]),
                str(stats["with_video"]),
                str(stats["with_metadata"]),
                status,
            ))
        except PermissionError as exc:
            rows_out.append((system, folder_name, "—", "—", "—", "—", f"permission denied (try sudo): {exc}"))
        except Exception as exc:
            rows_out.append((system, folder_name, "—", "—", "—", "—", f"ERROR: {exc}"))

    columns = ("System", "Folder", "Entries", "w/Image", "w/Video", "w/Metadata", "Output")
    if console:
        table = Table(title="gen-gamelist")
        for col in columns:
            table.add_column(col)
        for row in rows_out:
            table.add_row(*row)
        console.print(table)
    else:
        print(" | ".join(columns))
        for row in rows_out:
            print(" | ".join(row))
