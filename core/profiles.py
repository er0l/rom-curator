"""Device profile loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .mappings import get_target_aliases, get_system_display, screen_fit

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


@dataclass(frozen=True)
class ProfileIssue:
    level: str
    message: str


def load_profile(path: str | Path) -> dict[str, object]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read profiles. Install curator/requirements.txt") from exc

    profile_path = Path(path).expanduser()
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile does not exist: {profile_path}")

    with profile_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Profile root must be a mapping: {profile_path}")
    return data


def load_profiles(directory: str | Path) -> dict[str, dict[str, object]]:
    profiles_dir = Path(directory).expanduser()
    if not profiles_dir.exists():
        raise FileNotFoundError(f"Profiles directory does not exist: {profiles_dir}")

    profiles: dict[str, dict[str, object]] = {}
    for path in sorted(profiles_dir.glob("*.yaml")):
        profile = load_profile(path)
        name = str(profile.get("name") or path.stem)
        profile["_path"] = str(path)
        profiles[name] = profile
    return profiles


def validate_profile(
    profile: dict[str, object],
    mappings: dict[str, dict[str, object]],
    layouts: dict[str, dict[str, list[str]]] | None = None,
) -> list[ProfileIssue]:
    issues: list[ProfileIssue] = []
    name = str(profile.get("name") or "(unnamed)")
    target = profile.get("target")
    known_targets = sorted(layouts.keys()) if layouts else []
    if not isinstance(target, str) or (known_targets and target not in known_targets):
        label = ", ".join(known_targets) if known_targets else "a valid target"
        issues.append(ProfileIssue("error", f"{name}: target must be one of {label}"))
        return issues

    include_systems = _system_list(profile.get("include_systems"), mappings)
    exclude_systems = _as_string_list(profile.get("exclude_systems"))
    active_systems = [system for system in include_systems if system not in set(exclude_systems)]

    for key in ("include_systems", "exclude_systems", "preferred_region"):
        value = profile.get(key)
        if value is not None and value != "all" and not isinstance(value, list):
            issues.append(ProfileIssue("error", f"{name}: {key} must be a list"))

    max_games = profile.get("max_games_per_system")
    if max_games is not None and (not isinstance(max_games, int) or max_games <= 0):
        issues.append(ProfileIssue("error", f"{name}: max_games_per_system must be a positive integer"))

    unknown = sorted((set(include_systems) | set(exclude_systems)) - set(mappings))
    for system in unknown:
        issues.append(ProfileIssue("error", f"{name}: unknown system '{system}'"))

    if layouts is not None and isinstance(target, str):
        for system in active_systems:
            if system not in mappings:
                continue
            if not get_target_aliases(layouts, system, target):
                issues.append(ProfileIssue("warning", f"{name}: '{system}' has no {target} alias"))

    if profile.get("include_systems") != "all":
        overlap = sorted(set(include_systems) & set(exclude_systems))
        for system in overlap:
            issues.append(ProfileIssue("warning", f"{name}: '{system}' is both included and excluded"))

    return issues


def validate_profiles(
    profiles: dict[str, dict[str, object]],
    mappings: dict[str, dict[str, object]],
    layouts: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, list[ProfileIssue]]:
    return {
        name: validate_profile(profile, mappings, layouts)
        for name, profile in sorted(profiles.items())
    }


def selected_systems(profile: dict[str, object], mappings: dict[str, dict[str, object]]) -> list[str]:
    systems = _system_list(profile.get("include_systems"), mappings)
    excluded = set(_as_string_list(profile.get("exclude_systems")))
    return [system for system in systems if system not in excluded]


def print_profiles(
    profiles: dict[str, dict[str, object]],
    mappings: dict[str, dict[str, object]],
    validation: dict[str, list[ProfileIssue]],
) -> None:
    console = Console() if Console else None
    rows = []
    for name, profile in sorted(profiles.items()):
        systems = selected_systems(profile, mappings)
        issue_counts = _issue_counts(validation.get(name, []))
        rows.append(
            (
                name,
                str(profile.get("target", "-")),
                str(len(systems)),
                _format_optional(profile.get("max_games_per_system")),
                ", ".join(_as_string_list(profile.get("preferred_region"))) or "-",
                issue_counts,
            )
        )

    if console and Table:
        table = Table(title="Device Profiles")
        for column in ("Profile", "Target", "Systems", "Max/System", "Preferred Regions", "Validation"):
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        console.print(table)
        _print_validation(console, validation)
        return

    print("Device Profiles")
    print("===============")
    print("Profile | Target | Systems | Max/System | Preferred Regions | Validation")
    for row in rows:
        print(" | ".join(row))
    _print_validation_plain(validation)


def print_profile_detail(
    name: str,
    profile: dict[str, object],
    mappings: dict[str, dict[str, object]],
    issues: list[ProfileIssue],
    layouts: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    console = Console() if Console else None
    systems = selected_systems(profile, mappings)
    target = str(profile.get("target", "-"))
    screen = profile.get("screen") if isinstance(profile.get("screen"), dict) else None
    screen_w = int(screen["width"]) if screen else 0
    screen_h = int(screen["height"]) if screen else 0
    has_screen = screen_w > 0 and screen_h > 0

    rows = [
        (
            system,
            ", ".join(get_target_aliases(layouts or {}, system, target)) or "-",
            screen_fit(get_system_display(mappings, system), screen_w, screen_h) if has_screen else "-",
        )
        for system in systems
        if system in mappings
    ]

    if console and Table:
        console.print(f"[bold]{name}[/bold]")
        console.print(str(profile.get("description", "")))
        console.print(f"Target: [bold]{target}[/bold]")
        if screen:
            size = screen.get("size_inches", "?")
            console.print(f"Screen: {screen_w}x{screen_h} ({size}\") — ratio {screen_w/screen_h:.2f}:1")
        console.print(f"Preferred regions: {', '.join(_as_string_list(profile.get('preferred_region'))) or '-'}")
        console.print(f"Max games/system: {_format_optional(profile.get('max_games_per_system'))}")
        table = Table(title="Profile Systems")
        table.add_column("Canonical")
        table.add_column("Target Aliases")
        table.add_column("Fit")
        for row in rows:
            fit = row[2]
            fit_style = {"good": "green", "ok": "yellow", "poor": "red", "mixed": "cyan"}.get(fit, "")
            table.add_row(row[0], row[1], f"[{fit_style}]{fit}[/{fit_style}]" if fit_style else fit)
        console.print(table)
        _print_validation(console, {name: issues})
        return

    print(name)
    print(str(profile.get("description", "")))
    print(f"Target: {target}")
    if screen:
        size = screen.get("size_inches", "?")
        print(f"Screen: {screen_w}x{screen_h} ({size}\") — ratio {screen_w/screen_h:.2f}:1")
    print(f"Preferred regions: {', '.join(_as_string_list(profile.get('preferred_region'))) or '-'}")
    print(f"Max games/system: {_format_optional(profile.get('max_games_per_system'))}")
    print("Canonical | Target Aliases | Fit")
    for row in rows:
        print(" | ".join(row))
    _print_validation_plain({name: issues})


def modify_profile_systems(
    profile_path: str | Path,
    add: list[str],
    remove: list[str],
    mappings: dict[str, dict[str, object]],
) -> dict[str, list[str]]:
    """Add/remove systems from a profile YAML file in-place, preserving all comments.

    Returns a dict with keys 'added', 'removed', 'already_present', 'not_found',
    'unknown' (systems not in the mapping matrix).
    """
    path = Path(profile_path).expanduser()
    profile = load_profile(path)
    text = path.read_text(encoding="utf-8")

    include_raw = profile.get("include_systems")
    exclude_raw = profile.get("exclude_systems")
    include_all = include_raw == "all" or include_raw is None
    current_include: list[str] = [] if include_all else _as_string_list(include_raw)
    current_exclude: list[str] = _as_string_list(exclude_raw)

    unknown = [s for s in (add + remove) if s not in mappings]
    result: dict[str, list[str]] = {
        "added": [], "removed": [], "already_present": [], "not_found": [], "unknown": unknown,
    }

    new_include = list(current_include)
    new_exclude = list(current_exclude)

    for system in add:
        if system in unknown:
            continue
        if include_all or system in new_include:
            result["already_present"].append(system)
        else:
            new_include.append(system)
            result["added"].append(system)
        if system in new_exclude:
            new_exclude.remove(system)

    for system in remove:
        if system in unknown:
            continue
        if include_all:
            # include_systems = all: removing means adding to exclude_systems
            if system not in new_exclude:
                new_exclude.append(system)
                result["removed"].append(system)
            else:
                result["already_present"].append(system)
        elif system in new_include:
            new_include.remove(system)
            result["removed"].append(system)
        else:
            result["not_found"].append(system)

    # Only rewrite the file if something changed
    if result["added"] or result["removed"]:
        if not include_all:
            text = _rewrite_yaml_block_list(text, "include_systems", sorted(new_include))
        text = _rewrite_yaml_block_list(text, "exclude_systems", sorted(new_exclude))
        path.write_text(text, encoding="utf-8")

    return result


def _rewrite_yaml_block_list(text: str, key: str, values: list[str]) -> str:
    """Replace the YAML sequence under a root-level `key`, preserving everything else."""
    lines = text.splitlines(keepends=True)
    key_line = f"{key}:"

    # Find the root-level key
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == key_line:
            start = i
            break

    if start is None:
        # Key absent — append it before the final blank line if one exists
        suffix = "" if text.endswith("\n") else "\n"
        block = key_line + "\n" + "".join(f"  - {v}\n" for v in values) + "\n"
        return text + suffix + block

    # Find end of block: first non-blank line that starts at column 0
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line[0].isspace():
            break
        end += 1

    new_block = [key_line + "\n"] + [f"  - {v}\n" for v in values] + ["\n"]
    return "".join(lines[:start] + new_block + lines[end:])


def _system_list(value: object, mappings: dict[str, dict[str, object]]) -> list[str]:
    if value == "all" or value is None:
        return sorted(mappings)
    return _as_string_list(value)


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _format_optional(value: object) -> str:
    return "-" if value is None else str(value)


def _issue_counts(issues: list[ProfileIssue]) -> str:
    errors = sum(1 for issue in issues if issue.level == "error")
    warnings = sum(1 for issue in issues if issue.level == "warning")
    if not errors and not warnings:
        return "OK"
    return f"{errors} errors, {warnings} warnings"


def _print_validation(console, validation: dict[str, list[ProfileIssue]]) -> None:
    issues = [(name, issue) for name, items in validation.items() for issue in items]
    if not issues:
        console.print("Profile validation: OK", style="green")
        return

    table = Table(title="Profile Validation")
    table.add_column("Profile")
    table.add_column("Level")
    table.add_column("Message")
    for name, issue in issues:
        style = "red" if issue.level == "error" else "yellow"
        table.add_row(name, issue.level, issue.message, style=style)
    console.print(table)


def _print_validation_plain(validation: dict[str, list[ProfileIssue]]) -> None:
    issues = [(name, issue) for name, items in validation.items() for issue in items]
    if not issues:
        print("Profile validation: OK")
        return
    print("\nProfile Validation")
    print("------------------")
    for name, issue in issues:
        print(f"{name}: {issue.level}: {issue.message}")
