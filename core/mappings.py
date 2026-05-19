"""System mapping matrix loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


@dataclass(frozen=True)
class MappingIssue:
    level: str
    message: str


# ---------------------------------------------------------------------------
# systems.yaml — canonical definitions (nas + metadata only)
# ---------------------------------------------------------------------------

def load_system_mappings(path: str | Path) -> dict[str, dict[str, object]]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read system mappings. Install curator/requirements.txt") from exc

    mapping_path = Path(path).expanduser()
    if not mapping_path.exists():
        raise FileNotFoundError(f"System mappings file does not exist: {mapping_path}")

    with mapping_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError(f"System mappings root must be a mapping: {mapping_path}")

    return data


def validate_system_mappings(mappings: dict[str, dict[str, object]]) -> list[MappingIssue]:
    """Validate canonical system definitions (nas + metadata only)."""
    issues: list[MappingIssue] = []
    nas_seen: dict[str, str] = {}

    for canonical, row in sorted(mappings.items()):
        if not isinstance(canonical, str) or not canonical.strip():
            issues.append(MappingIssue("error", f"Invalid canonical key: {canonical!r}"))
            continue
        if not isinstance(row, dict):
            issues.append(MappingIssue("error", f"{canonical}: mapping row must be a mapping"))
            continue

        nas = row.get("nas")
        if not nas:
            issues.append(MappingIssue("error", f"{canonical}: missing 'nas' folder name"))
            continue

        nas_str = str(nas)
        previous = nas_seen.get(nas_str)
        if previous and previous != canonical:
            issues.append(MappingIssue(
                "warning",
                f"nas folder '{nas_str}' is used by both '{previous}' and '{canonical}'",
            ))
        nas_seen[nas_str] = canonical

    return issues


# ---------------------------------------------------------------------------
# layouts/ — per-device folder aliases
# ---------------------------------------------------------------------------

def load_layouts(layouts_dir: str | Path) -> dict[str, dict[str, list[str]]]:
    """Load all layout YAML files from *layouts_dir*.

    Returns a dict mapping ``target_name → {canonical → [alias, ...]}``.
    Files are named ``<target>.yaml``.  Missing or empty lists are omitted.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read layout files. Install curator/requirements.txt") from exc

    layouts_path = Path(layouts_dir).expanduser()
    layouts: dict[str, dict[str, list[str]]] = {}

    if not layouts_path.exists():
        return layouts

    for layout_file in sorted(layouts_path.glob("*.yaml")):
        target = layout_file.stem
        with layout_file.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            continue
        entry: dict[str, list[str]] = {}
        for canonical, value in data.items():
            aliases = _as_alias_list(value)
            if aliases:
                entry[str(canonical)] = aliases
        layouts[target] = entry

    return layouts


def validate_layouts(
    layouts: dict[str, dict[str, list[str]]],
    mappings: dict[str, dict[str, object]],
) -> list[MappingIssue]:
    """Check that every canonical in any layout file exists in *mappings*."""
    issues: list[MappingIssue] = []
    for target, entry in sorted(layouts.items()):
        alias_seen: dict[str, str] = {}
        for canonical, aliases in sorted(entry.items()):
            if canonical not in mappings:
                issues.append(MappingIssue(
                    "warning",
                    f"layouts/{target}.yaml: unknown canonical '{canonical}'",
                ))
            for alias in aliases:
                if not alias.strip():
                    issues.append(MappingIssue(
                        "error",
                        f"layouts/{target}.yaml: empty alias for '{canonical}'",
                    ))
                    continue
                previous = alias_seen.get(alias)
                if previous and previous != canonical:
                    issues.append(MappingIssue(
                        "warning",
                        f"layouts/{target}.yaml: alias '{alias}' used by both "
                        f"'{previous}' and '{canonical}'",
                    ))
                alias_seen[alias] = canonical
    return issues


# ---------------------------------------------------------------------------
# Alias accessors — operate on *layouts*, not *mappings*
# ---------------------------------------------------------------------------

def get_target_aliases(
    layouts: dict[str, dict[str, list[str]]],
    canonical: str,
    target: str,
) -> list[str]:
    """Return folder aliases for *canonical* under *target* (e.g. ``'r36s'``).

    Falls back to an empty list when the target layout has no entry.
    """
    entry = layouts.get(target, {})
    return list(entry.get(canonical, []))


def get_preferred_alias(
    layouts: dict[str, dict[str, list[str]]],
    canonical: str,
    target: str,
) -> str | None:
    """Return the first (preferred) alias for *canonical* under *target*."""
    aliases = get_target_aliases(layouts, canonical, target)
    return aliases[0] if aliases else None


# ---------------------------------------------------------------------------
# Reverse lookup
# ---------------------------------------------------------------------------

def find_canonical_system(
    mappings: dict[str, dict[str, object]],
    alias: str,
    target: str = "nas",
    *,
    layouts: dict[str, dict[str, list[str]]] | None = None,
) -> str | None:
    """Return the canonical name whose *target* aliases contain *alias*.

    For ``target='nas'`` the lookup is done directly against *mappings*.
    For all other targets *layouts* must be provided; returns ``None`` when
    the target is not present in *layouts*.
    """
    if target == "nas":
        for canonical, row in mappings.items():
            if _as_alias_list(row.get("nas")) and alias in _as_alias_list(row.get("nas")):
                return canonical
        return None

    if layouts is None:
        return None
    entry = layouts.get(target, {})
    for canonical, aliases in entry.items():
        if alias in aliases:
            return canonical
    return None


# ---------------------------------------------------------------------------
# Display / screen-fit helpers (unchanged — operate on mappings)
# ---------------------------------------------------------------------------

def get_system_display(mappings: dict[str, dict[str, object]], canonical: str) -> dict | None:
    """Return the display metadata dict for a system, or None if not annotated."""
    row = mappings.get(canonical)
    if not row:
        return None
    display = row.get("display")
    return display if isinstance(display, dict) else None


def screen_fit(display: dict | None, screen_w: int, screen_h: int) -> str:
    """Return 'good', 'ok', 'poor', 'mixed', or '-' for how well a system fits a screen."""
    if not display:
        return "-"
    orientation = str(display.get("orientation", "landscape"))
    if orientation == "mixed":
        return "mixed"
    sys_ratio = _parse_aspect(str(display.get("aspect", "")))
    if sys_ratio is None:
        return "-"
    screen_ratio = screen_w / screen_h
    sys_is_portrait = orientation == "portrait" or sys_ratio < 0.9
    screen_is_portrait = screen_ratio < 0.9
    if sys_is_portrait != screen_is_portrait:
        return "poor"
    fill = (screen_ratio / sys_ratio) if sys_ratio >= screen_ratio else (sys_ratio / screen_ratio)
    if fill >= 0.88:
        return "good"
    if fill >= 0.70:
        return "ok"
    return "poor"


def _parse_aspect(aspect: str) -> float | None:
    try:
        a, b = aspect.split(":")
        return float(a) / float(b)
    except (ValueError, ZeroDivisionError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------

def print_system_mappings(
    mappings: dict[str, dict[str, object]],
    issues: list[MappingIssue],
    *,
    layouts: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    console = Console() if Console else None
    layout_targets = sorted(layouts.keys()) if layouts else []

    rows = []
    for canonical, row in sorted(mappings.items()):
        cells = [canonical, _format_aliases(row.get("nas"))]
        for t in layout_targets:
            cells.append(_format_aliases(layouts[t].get(canonical)))  # type: ignore[index]
        rows.append(tuple(cells))

    headers = ["Canonical", "NAS"] + [t.upper() for t in layout_targets]

    if console and Table:
        table = Table(title="System Mapping Matrix")
        for h in headers:
            table.add_column(h)
        for row in rows:
            table.add_row(*row)
        console.print(table)
        _print_issues_rich(console, issues)
        return

    print("System Mapping Matrix")
    print("=====================")
    print(" | ".join(headers))
    for row in rows:
        print(" | ".join(row))
    _print_issues_plain(issues)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _as_alias_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _format_aliases(value: object) -> str:
    aliases = _as_alias_list(value)
    return ", ".join(aliases) if aliases else "-"


def _print_issues_rich(console, issues: list[MappingIssue]) -> None:
    if not issues:
        console.print("Mapping validation: OK", style="green")
        return

    table = Table(title="Mapping Validation")
    table.add_column("Level")
    table.add_column("Message")
    for issue in issues:
        style = "red" if issue.level == "error" else "yellow"
        table.add_row(issue.level, issue.message, style=style)
    console.print(table)


def _print_issues_plain(issues: list[MappingIssue]) -> None:
    if not issues:
        print("Mapping validation: OK")
        return
    print("\nMapping Validation")
    print("------------------")
    for issue in issues:
        print(f"{issue.level}: {issue.message}")
