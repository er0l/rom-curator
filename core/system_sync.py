"""Sync ROM system folders against mappings and device profiles.

Scans the roms root for subdirectories, cross-references against
mappings/systems.yaml, and for each profile reports:

  - New systems  — folder exists + in mappings, but missing from the profile
  - Removed      — listed in the profile's include_systems, but folder is gone

Dry-run by default.  Pass apply=True (--apply on the CLI) to write changes.
"""

from __future__ import annotations

from pathlib import Path

from .profiles import modify_profile_systems, selected_systems

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


def run_scan_systems(
    config: dict[str, object],
    *,
    mappings: dict[str, dict[str, object]],
    profiles: dict[str, dict[str, object]],
    apply: bool = False,
) -> None:
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")

    roms_root = Path(str(paths["roms"])).expanduser()
    if not roms_root.exists():
        raise FileNotFoundError(f"ROM root not found: {roms_root}")

    console = Console() if Console else None

    # ── 1. Scan ROM root ────────────────────────────────────────────────────
    existing_folders: set[str] = {d.name for d in roms_root.iterdir() if d.is_dir()}

    known_with_folder    = sorted(s for s in mappings if s in existing_folders)
    known_without_folder = sorted(s for s in mappings if s not in existing_folders)
    unknown_folders      = sorted(f for f in existing_folders if f not in mappings)

    # ── 2. Per-profile diff ─────────────────────────────────────────────────
    # For profiles with include_systems = all, new systems are automatically
    # included (they're already in mappings) so we skip them.
    # Gone-folder systems might linger harmlessly for "all" profiles but we
    # leave those alone too — there's nothing stale to clean up in the YAML.
    profile_diffs: dict[str, dict] = {}

    for profile_name, profile in sorted(profiles.items()):
        include_raw = profile.get("include_systems")
        if include_raw == "all" or include_raw is None:
            continue  # fully-open profile — nothing to manage

        current_include: set[str] = set(
            include_raw if isinstance(include_raw, list) else []
        )

        to_add    = sorted(s for s in known_with_folder    if s not in current_include)
        to_remove = sorted(s for s in current_include
                           if s in mappings and s not in existing_folders)

        if to_add or to_remove:
            profile_diffs[profile_name] = {
                "path":   profile.get("_path"),
                "add":    to_add,
                "remove": to_remove,
            }

    # ── 3. Report ───────────────────────────────────────────────────────────
    mode = "APPLY" if apply else "DRY RUN"
    _print(console, f"\nSystem Scan — {mode}")
    _print(console, f"ROM root: {roms_root}\n")

    # Summary table
    _print_table(
        console,
        "Summary",
        ["Category", "Count"],
        [
            ("Known systems with ROM folder",    len(known_with_folder)),
            ("Known systems without ROM folder", len(known_without_folder)),
            ("Unknown folders (not in mappings)", len(unknown_folders)),
        ],
    )

    if unknown_folders:
        _print(console, "Unknown folders (add to mappings/systems.yaml to manage):")
        for f in unknown_folders:
            _print(console, f"  {f}")
        _print(console, "")

    if not profile_diffs:
        _print(console, "All profiles are already in sync with the ROM library.")
        return

    # Per-profile diff table
    rows = []
    for profile_name, diff in profile_diffs.items():
        for s in diff["add"]:
            rows.append((profile_name, s, "ADD"))
        for s in diff["remove"]:
            rows.append((profile_name, s, "REMOVE"))

    _print_table(
        console,
        f"Profile Changes ({'would apply' if not apply else 'applied'})",
        ["Profile", "System", "Action"],
        rows,
    )

    if not apply:
        _print(console, f"\nDRY RUN — {len(rows)} change(s) across {len(profile_diffs)} profile(s). "
               "Pass --apply to update profiles.")
        return

    # ── 4. Apply ────────────────────────────────────────────────────────────
    total_added = total_removed = total_errors = 0
    for profile_name, diff in profile_diffs.items():
        if not diff["path"]:
            _print(console, f"  SKIP  {profile_name}: profile path unknown", style="yellow")
            continue
        try:
            result = modify_profile_systems(
                diff["path"],
                add=diff["add"],
                remove=diff["remove"],
                mappings=mappings,
            )
            added   = len(result.get("added",   []))
            removed = len(result.get("removed", []))
            total_added   += added
            total_removed += removed
            if added or removed:
                _print(console,
                       f"  OK    {profile_name}: +{added} added, -{removed} removed",
                       style="green")
        except Exception as exc:
            total_errors += 1
            _print(console, f"  ERROR {profile_name}: {exc}", style="red")

    style = "red" if total_errors else "green"
    _print(console,
           f"\nDone — added: {total_added}  removed: {total_removed}  errors: {total_errors}",
           style=style)


# ── Helpers ──────────────────────────────────────────────────────────────────

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
