"""ROM system folder discovery and profile comparison.

scan_systems   — scan roms root, report new/missing/unknown folders vs mappings
compare_systems — compare discovered folders against one profile's include list
"""

from __future__ import annotations

from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


# ── Shared folder scanner ─────────────────────────────────────────────────────

def _scan_folders(config: dict[str, object]) -> tuple[Path, set[str]]:
    """Return (roms_root, set_of_visible_subfolder_names) applying config exclusions."""
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root = Path(str(paths["roms"])).expanduser()
    if not roms_root.exists():
        raise FileNotFoundError(f"ROM root not found: {roms_root}")

    scan_cfg = config.get("scan", {})
    ignore_hidden = bool(scan_cfg.get("ignore_hidden", True)) if isinstance(scan_cfg, dict) else True
    exclude_list: list[str] = []
    if isinstance(scan_cfg, dict):
        raw = scan_cfg.get("exclude_system_folders", [])
        if isinstance(raw, list):
            exclude_list = [str(e) for e in raw]

    excluded: set[str] = set(exclude_list)

    folders: set[str] = set()
    for d in roms_root.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        if name in excluded:
            continue
        if ignore_hidden and name.startswith("."):
            continue
        folders.add(name)

    return roms_root, folders


# ── scan-systems ─────────────────────────────────────────────────────────────

def run_scan_systems(
    config: dict[str, object],
    *,
    mappings: dict[str, dict[str, object]],
) -> None:
    """Scan roms root and report what's new, missing, or unknown vs mappings."""
    console = Console() if Console else None

    roms_root, existing_folders = _scan_folders(config)

    scan_cfg = config.get("scan", {})
    exclude_list: list[str] = []
    if isinstance(scan_cfg, dict):
        raw = scan_cfg.get("exclude_system_folders", [])
        if isinstance(raw, list):
            exclude_list = [str(e) for e in raw]

    known_with_folder    = sorted(s for s in mappings if s in existing_folders)
    known_without_folder = sorted(s for s in mappings if s not in existing_folders)
    unknown_folders      = sorted(f for f in existing_folders if f not in mappings)

    _print(console, f"\nSystem Scan: {roms_root}")
    if exclude_list:
        _print(console, f"Excluded:    {', '.join(exclude_list)}")
    _print(console, "")

    _print_table(
        console,
        "Summary",
        ["Category", "Count"],
        [
            ("Known systems — folder present",  len(known_with_folder)),
            ("Known systems — folder absent",   len(known_without_folder)),
            ("Unknown folders (not in mappings)", len(unknown_folders)),
        ],
    )

    if known_with_folder:
        _print(console, "\nKnown systems with folder:")
        _print(console, "  " + "  ".join(known_with_folder))

    if known_without_folder:
        _print(console, "\nKnown systems WITHOUT folder (defined in mappings but no directory):")
        _print(console, "  " + "  ".join(known_without_folder))

    if unknown_folders:
        _print(console, "\nUnknown folders (not in mappings/systems.yaml):")
        for f in unknown_folders:
            _print(console, f"  {f}")
        _print(console, "\n  → Add these to mappings/systems.yaml, or add to")
        _print(console,   "    scan.exclude_system_folders in config.yaml to silence this warning.")


# ── compare-systems ───────────────────────────────────────────────────────────

def run_compare_systems(
    config: dict[str, object],
    profile_name: str,
    *,
    mappings: dict[str, dict[str, object]],
    profiles: dict[str, dict[str, object]],
) -> None:
    """Compare discovered system folders against one profile's include list."""
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise KeyError(f"Unknown profile '{profile_name}'. Available: {available}")

    console = Console() if Console else None
    profile = profiles[profile_name]
    roms_root, existing_folders = _scan_folders(config)

    include_raw = profile.get("include_systems")
    include_all = include_raw == "all" or include_raw is None
    current_include: set[str] = (
        set(mappings) if include_all
        else set(include_raw if isinstance(include_raw, list) else [])
    )

    # Only consider systems known to mappings for the comparison
    known_present = {s for s in mappings if s in existing_folders}

    included      = sorted(known_present & current_include)
    not_included  = sorted(known_present - current_include)
    missing       = sorted(
        s for s in current_include
        if s in mappings and s not in existing_folders
    )

    _print(console, f"\nCompare systems — profile: {profile_name}")
    _print(console, f"ROM root: {roms_root}\n")

    _print_table(
        console,
        "Summary",
        ["Category", "Count"],
        [
            ("Included in profile (folder present)",       len(included)),
            ("Not in profile   (folder present, can add)", len(not_included)),
            ("In profile but folder missing  (can remove)", len(missing)),
        ],
    )

    if included:
        _print(console, "\nIncluded:")
        _print(console, "  " + "  ".join(included))

    if not_included:
        _print(console, "\nNot in profile — available to add:")
        _print(console, "  " + "  ".join(not_included))
        hint = ",".join(not_included)
        _print(console, f"\n  → romcurator profile-add {profile_name} {hint}")

    if missing:
        _print(console, "\nIn profile but folder is gone:")
        _print(console, "  " + "  ".join(missing))
        hint = ",".join(missing)
        _print(console, f"\n  → romcurator profile-remove {profile_name} {hint}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        print(msg)


def _print_table(console, title: str, columns: list[str], rows: list[tuple]) -> None:
    if console and Table:
        table = Table(title=title)
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*(str(v) for v in row))
        console.print(table)
        return
    print(f"\n{title}")
    print("-" * len(title))
    print(" | ".join(columns))
    for row in rows:
        print(" | ".join(str(v) for v in row))
