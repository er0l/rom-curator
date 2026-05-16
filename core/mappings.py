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


TARGETS = ("nas", "romm", "emudeck", "r36s", "batocera")


@dataclass(frozen=True)
class MappingIssue:
    level: str
    message: str


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
    issues: list[MappingIssue] = []
    aliases_by_target: dict[str, dict[str, str]] = {target: {} for target in TARGETS}

    for canonical, row in sorted(mappings.items()):
        if not isinstance(canonical, str) or not canonical.strip():
            issues.append(MappingIssue("error", f"Invalid canonical key: {canonical!r}"))
            continue
        if not isinstance(row, dict):
            issues.append(MappingIssue("error", f"{canonical}: mapping row must be a mapping"))
            continue

        for target in TARGETS:
            if target not in row:
                issues.append(MappingIssue("error", f"{canonical}: missing target '{target}'"))
                continue

            aliases = _as_alias_list(row[target])
            if target == "nas" and len(aliases) != 1:
                issues.append(MappingIssue("error", f"{canonical}: 'nas' must contain exactly one alias"))
            if not aliases and target == "nas":
                continue

            for alias in aliases:
                if not alias.strip():
                    issues.append(MappingIssue("error", f"{canonical}: empty alias in '{target}'"))
                    continue

                previous = aliases_by_target[target].get(alias)
                if previous and previous != canonical:
                    issues.append(
                        MappingIssue(
                            "warning",
                            f"{target}: alias '{alias}' is used by both '{previous}' and '{canonical}'",
                        )
                    )
                aliases_by_target[target][alias] = canonical

    return issues


def get_target_aliases(
    mappings: dict[str, dict[str, object]],
    canonical: str,
    target: str,
) -> list[str]:
    if target not in TARGETS:
        raise ValueError(f"Unknown mapping target: {target}")
    row = mappings.get(canonical)
    if row is None:
        raise KeyError(f"Unknown canonical system: {canonical}")
    return _as_alias_list(row.get(target))


def get_preferred_alias(
    mappings: dict[str, dict[str, object]],
    canonical: str,
    target: str,
) -> str | None:
    aliases = get_target_aliases(mappings, canonical, target)
    return aliases[0] if aliases else None


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


def find_canonical_system(
    mappings: dict[str, dict[str, object]],
    alias: str,
    target: str = "nas",
) -> str | None:
    if target not in TARGETS:
        raise ValueError(f"Unknown mapping target: {target}")
    for canonical, row in mappings.items():
        if alias in _as_alias_list(row.get(target)):
            return canonical
    return None


def print_system_mappings(mappings: dict[str, dict[str, object]], issues: list[MappingIssue]) -> None:
    console = Console() if Console else None
    rows = [
        (
            canonical,
            _format_aliases(row.get("nas")),
            _format_aliases(row.get("romm")),
            _format_aliases(row.get("emudeck")),
            _format_aliases(row.get("r36s")),
            _format_aliases(row.get("batocera")),
        )
        for canonical, row in sorted(mappings.items())
    ]

    if console and Table:
        table = Table(title="System Mapping Matrix")
        for column in ("Canonical", "NAS", "ROMM", "EmuDeck", "R36S", "Batocera"):
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        console.print(table)
        _print_issues_rich(console, issues)
        return

    print("System Mapping Matrix")
    print("=====================")
    print("Canonical | NAS | ROMM | EmuDeck | R36S | Batocera")
    for row in rows:
        print(" | ".join(row))
    _print_issues_plain(issues)


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
