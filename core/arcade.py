"""Arcade game classification using MAME XML metadata."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import iterparse

from .database import InventoryDatabase

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
except ImportError:  # pragma: no cover
    Console = None
    Progress = None
    SpinnerColumn = None
    TextColumn = None
    TimeElapsedColumn = None


# Maps MAME sourcefile path → canonical arcade sub-system name.
# Everything not listed here falls into the generic 'mame' bucket.
SOURCEFILE_TO_SYSTEM: dict[str, str] = {
    "capcom/cps1.cpp": "cps1",
    "capcom/cps2.cpp": "cps2",
    "capcom/cps3.cpp": "cps3",
    "neogeo/neogeo.cpp": "neogeo",
    "sega/naomi.cpp": "naomi",
    "sega/naomi2.cpp": "naomi2",
    "sega/atomiswave.cpp": "atomiswave",
}

PLAYABLE_STATUSES = frozenset({"good", "imperfect"})
BATCH_SIZE = 1000


@dataclass(frozen=True)
class MameMachine:
    name: str
    description: str
    year: str | None
    manufacturer: str | None
    sourcefile: str | None
    arcade_system: str          # classified: cps1, cps2, neogeo, mame, …
    cloneof: str | None         # None = parent ROM
    isbios: bool
    isdevice: bool
    ismechanical: bool
    runnable: bool
    driver_status: str | None   # good / imperfect / preliminary / None
    players: int | None
    control_types: list[str]    # e.g. ['joy', 'joy']
    display_type: str | None    # raster / vector / lcd / svg
    display_rotate: int | None  # 0 / 90 / 180 / 270

    @property
    def is_parent(self) -> bool:
        return self.cloneof is None

    @property
    def is_playable(self) -> bool:
        return (
            not self.isbios
            and not self.isdevice
            and not self.ismechanical
            and self.runnable
            and self.driver_status in PLAYABLE_STATUSES
        )

    @property
    def is_vertical(self) -> bool:
        """True for vertical-orientation games (rotate 90° or 270°)."""
        return self.display_rotate in (90, 270)


class ArcadeClassifier:
    """Classifies arcade ROMs by sub-system using cached MAME machine data."""

    def __init__(self, database_path: str | Path) -> None:
        self._db_path = Path(database_path)
        self._cache: dict[str, str] | None = None

    def get_system(self, rom_name: str) -> str | None:
        """Return the arcade sub-system for a ROM stem (e.g. 'sf2' → 'cps2'), or None."""
        if self._cache is None:
            self._load_cache()
        return self._cache.get(rom_name)

    def _load_cache(self) -> None:
        with InventoryDatabase(self._db_path) as db:
            db.initialize()
            rows = db.fetch_all("SELECT name, arcade_system FROM mame_machines")
        self._cache = {str(row[0]): str(row[1]) for row in rows}


# ---------------------------------------------------------------------------
# Sourcefile classification
# ---------------------------------------------------------------------------

def classify_sourcefile(sourcefile: str | None) -> str:
    """Map a MAME sourcefile path to a canonical arcade sub-system name."""
    if sourcefile:
        system = SOURCEFILE_TO_SYSTEM.get(sourcefile)
        if system:
            return system
    return "mame"


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def parse_machine(elem) -> MameMachine:
    """Parse a <machine> lxml/ElementTree element into a MameMachine."""
    driver = elem.find("driver")
    inp = elem.find("input")
    display = elem.find("display")

    sourcefile = elem.get("sourcefile")
    arcade_system = classify_sourcefile(sourcefile)

    players: int | None = None
    controls: list[str] = []
    if inp is not None:
        try:
            players = int(inp.get("players") or 0) or None
        except (ValueError, TypeError):
            pass
        controls = [
            c.get("type", "")
            for c in inp.findall("control")
            if c.get("type")
        ]

    display_type: str | None = None
    display_rotate: int | None = None
    if display is not None:
        display_type = display.get("type")
        try:
            display_rotate = int(display.get("rotate") or 0)
        except (ValueError, TypeError):
            display_rotate = 0

    return MameMachine(
        name=elem.get("name", ""),
        description=elem.findtext("description") or "",
        year=elem.findtext("year"),
        manufacturer=elem.findtext("manufacturer"),
        sourcefile=sourcefile,
        arcade_system=arcade_system,
        cloneof=elem.get("cloneof"),
        isbios=elem.get("isbios") == "yes",
        isdevice=elem.get("isdevice") == "yes",
        ismechanical=elem.get("ismechanical") == "yes",
        runnable=elem.get("runnable", "yes") != "no",
        driver_status=driver.get("status") if driver is not None else None,
        players=players,
        control_types=controls,
        display_type=display_type,
        display_rotate=display_rotate,
    )


# ---------------------------------------------------------------------------
# Import command
# ---------------------------------------------------------------------------

def run_arcade_import(
    config: dict[str, object],
    *,
    xml_path: str | Path | None = None,
    reset: bool = False,
    version: str | None = None,
) -> dict[str, int]:
    """Parse MAME XML and populate mame_machines (or a versioned romset index).

    When *version* is given (e.g. 'mame2003', 'mame2003-plus'), only machine
    names are stored in mame_version_machines for that version label.  This
    lightweight index is used by the exporter to restrict arcade ROMs to the
    machines supported by a specific libretro core.

    Without *version*, the full machine metadata is written to mame_machines
    and arcade ROMs in the inventory are classified as before.
    """
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    database_path = Path(str(paths["database"])).expanduser()
    console = Console() if Console else None

    xml_path, source_desc = _resolve_xml_source(xml_path)

    if console:
        console.print(f"Source:   [bold]{source_desc}[/bold]")
        console.print(f"Database: [bold]{database_path}[/bold]")
        if version:
            console.print(f"Version:  [bold]{version}[/bold]  (version index only)")
    else:
        print(f"Source:   {source_desc}")
        print(f"Database: {database_path}")
        if version:
            print(f"Version:  {version}  (version index only)")

    with InventoryDatabase(database_path) as db:
        db.initialize()

        if version:
            # Version-index mode: store only machine names for this romset.
            if reset:
                db.connection.execute(
                    "DELETE FROM mame_version_machines WHERE version = ?", (version,)
                )
                db.connection.commit()
                msg = f"mame_version_machines cleared for version '{version}'."
                print(msg) if not console else console.print(msg)

            counts = _parse_and_import_version(db, xml_path, version, console)
            db.commit()
            _print_version_import_summary(counts, version, console)
            return counts

        # Full-metadata mode (existing behaviour)
        if reset:
            db.connection.execute("DELETE FROM mame_machines")
            db.connection.commit()
            msg = "mame_machines table cleared."
            print(msg) if not console else console.print(msg)

        counts = _parse_and_import(db, xml_path, console)
        classified = db.update_arcade_systems()
        db.commit()

    counts["arcade_roms_classified"] = classified
    _print_import_summary(counts, console)
    return counts


def _resolve_xml_source(xml_path) -> tuple[Path | None, str]:
    """Return (path_or_None, description). None = stream from mame binary."""
    if xml_path:
        p = Path(xml_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"MAME XML not found: {p}")
        return p, str(p)
    mame_bin = shutil.which("mame") or shutil.which("mame64")
    if not mame_bin:
        # Try common install paths
        for candidate in ("/usr/games/mame", "/usr/bin/mame", "/usr/local/bin/mame"):
            if Path(candidate).exists():
                mame_bin = candidate
                break
    if not mame_bin:
        raise RuntimeError(
            "MAME binary not found. Either install MAME or provide --xml /path/to/mame.xml\n"
            "  Generate the file with:  mame -listxml > mame.xml"
        )
    return None, f"{mame_bin} -listxml (streaming)"


def _parse_and_import(
    db: InventoryDatabase,
    xml_path: Path | None,
    console,
) -> dict[str, int]:
    """Stream-parse MAME XML and upsert every machine into mame_machines."""
    imported = 0
    systems: dict[str, int] = {}
    imported_at = int(time.time())

    def _process(source):
        nonlocal imported
        for event, elem in iterparse(source, events=["end"]):
            if elem.tag != "machine":
                # Do NOT clear child elements here — their text is needed when
                # the parent <machine> end event fires.  Clearing a <year> or
                # <manufacturer> element before the machine is processed would
                # destroy its text content.
                continue
            machine = parse_machine(elem)
            elem.clear()  # Free memory only after the machine has been parsed
            db.upsert_mame_machine(machine, imported_at)
            systems[machine.arcade_system] = systems.get(machine.arcade_system, 0) + 1
            imported += 1
            if imported % BATCH_SIZE == 0:
                db.commit()

    if Progress and console:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("{task.completed} machines"),
            TimeElapsedColumn(),
            console=console,
        )
        with progress:
            task = progress.add_task("Parsing MAME XML", total=None)

            def _process_with_progress(source):
                nonlocal imported
                for event, elem in iterparse(source, events=["end"]):
                    if elem.tag != "machine":
                        continue
                    machine = parse_machine(elem)
                    elem.clear()
                    db.upsert_mame_machine(machine, imported_at)
                    systems[machine.arcade_system] = systems.get(machine.arcade_system, 0) + 1
                    imported += 1
                    progress.advance(task)
                    if imported % BATCH_SIZE == 0:
                        db.commit()

            if xml_path:
                with xml_path.open("rb") as fh:
                    _process_with_progress(fh)
            else:
                mame_bin = shutil.which("mame") or shutil.which("mame64")
                for candidate in ("/usr/games/mame", "/usr/bin/mame", "/usr/local/bin/mame"):
                    if not mame_bin and Path(candidate).exists():
                        mame_bin = candidate
                with subprocess.Popen(
                    [mame_bin, "-listxml"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ) as proc:
                    _process_with_progress(proc.stdout)
    else:
        if xml_path:
            with xml_path.open("rb") as fh:
                _process(fh)
        else:
            mame_bin = shutil.which("mame") or shutil.which("mame64")
            for candidate in ("/usr/games/mame", "/usr/bin/mame", "/usr/local/bin/mame"):
                if not mame_bin and Path(candidate).exists():
                    mame_bin = candidate
            with subprocess.Popen(
                [mame_bin, "-listxml"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as proc:
                _process(proc.stdout)

    db.commit()
    return {"imported": imported, "systems": systems}


def _parse_and_import_version(
    db: InventoryDatabase,
    xml_path: Path | None,
    version: str,
    console,
) -> dict[str, int]:
    """Stream-parse MAME XML and insert machine names into mame_version_machines."""
    imported = 0

    def _process(source):
        nonlocal imported
        for event, elem in iterparse(source, events=["end"]):
            if elem.tag != "machine":
                continue
            name = elem.get("name", "")
            elem.clear()
            if name:
                db.upsert_mame_version_machine(version, name)
                imported += 1
                if imported % BATCH_SIZE == 0:
                    db.commit()

    if Progress and console:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("{task.completed} machines"),
            TimeElapsedColumn(),
            console=console,
        )
        with progress:
            task = progress.add_task(f"Parsing MAME XML [{version}]", total=None)

            def _process_with_progress(source):
                nonlocal imported
                for event, elem in iterparse(source, events=["end"]):
                    if elem.tag != "machine":
                        continue
                    name = elem.get("name", "")
                    elem.clear()
                    if name:
                        db.upsert_mame_version_machine(version, name)
                        imported += 1
                        progress.advance(task)
                        if imported % BATCH_SIZE == 0:
                            db.commit()

            if xml_path:
                with xml_path.open("rb") as fh:
                    _process_with_progress(fh)
            else:
                mame_bin = shutil.which("mame") or shutil.which("mame64")
                for candidate in ("/usr/games/mame", "/usr/bin/mame", "/usr/local/bin/mame"):
                    if not mame_bin and Path(candidate).exists():
                        mame_bin = candidate
                with subprocess.Popen(
                    [mame_bin, "-listxml"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ) as proc:
                    _process_with_progress(proc.stdout)
    else:
        if xml_path:
            with xml_path.open("rb") as fh:
                _process(fh)
        else:
            mame_bin = shutil.which("mame") or shutil.which("mame64")
            for candidate in ("/usr/games/mame", "/usr/bin/mame", "/usr/local/bin/mame"):
                if not mame_bin and Path(candidate).exists():
                    mame_bin = candidate
            with subprocess.Popen(
                [mame_bin, "-listxml"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as proc:
                _process(proc.stdout)

    db.commit()
    return {"imported": imported, "version": version}


def _print_version_import_summary(counts: dict, version: str, console) -> None:
    text = (
        f"arcade-import (version index) complete\n"
        f"Version:           {version}\n"
        f"Machines indexed:  {counts.get('imported', 0)}"
    )
    if console:
        console.print(text, style="green")
    else:
        print(text)


def _print_import_summary(counts: dict, console) -> None:
    imported = counts.get("imported", 0)
    classified = counts.get("arcade_roms_classified", 0)
    systems = counts.get("systems", {})

    lines = [
        "arcade-import complete",
        f"Machines imported:       {imported}",
        f"Arcade ROMs classified:  {classified}",
        "",
        "Machines by sub-system:",
    ]
    for system, count in sorted(systems.items(), key=lambda x: -x[1]):
        lines.append(f"  {system:<20s} {count}")

    text = "\n".join(lines)
    if console:
        console.print(text, style="green")
    else:
        print(text)
