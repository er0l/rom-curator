"""Compatibility list support for hardware-limited devices.

Compat lists map ROM stems or normalised game titles to a playability rating
tested on a specific chip (e.g. rk3326).  They are stored as YAML files under
mappings/compat/{chip}/{system}.yaml and referenced from profiles via:

  selection:
    compat_chip: rk3326
    compat_min_playability: Ok        # Good / Ok / Ok/Medium / Medium / Mediocre
    compat_include_unlisted: true     # include ROMs absent from the list (default)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

# Higher rank = better playability.
PLAYABILITY_RANK: dict[str, int] = {
    "Good":       5,
    "Ok":         4,
    "Ok/Medium":  3,
    "Medium":     2,
    "Mediocre":   1,
    "None":       0,
}

PLAYABILITY_LEVELS = list(PLAYABILITY_RANK.keys())


def normalise_key(s: str) -> str:
    """Lowercase, strip non-alphanumeric, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class CompatList:
    system: str
    chip: str
    match_by: str           # "stem" or "title"
    games: dict[str, str] = field(default_factory=dict)  # normalised_key → playability


def passes_compat(
    compat: CompatList | None,
    row,
    min_level: str,
    include_unlisted: bool,
) -> bool:
    """Return True when the ROM should be included (passes the compat filter).

    When compat is None (no list loaded for this system), always returns True.
    """
    if compat is None:
        return True

    if compat.match_by == "stem":
        filename = str(row["filename"])
        # Strip extension(s) — handle multi-part like .tar.gz via rstrip approach
        key = Path(filename).stem.lower()
    else:
        key = normalise_key(str(row["title"]))

    playability = compat.games.get(key)
    if playability is None:
        return include_unlisted
    return PLAYABILITY_RANK.get(playability, -1) >= PLAYABILITY_RANK.get(min_level, 0)


def load_compat_lists(mappings_dir: Path, chip: str) -> dict[str, CompatList]:
    """Load all compat YAML files for *chip* from mappings_dir/compat/{chip}/.

    Returns a dict keyed by canonical system name.  Missing or empty dir
    returns an empty dict — callers should treat that as "no compat data".
    """
    if _yaml is None:
        raise ImportError("PyYAML is required for compat list support")

    compat_dir = mappings_dir / "compat" / chip
    if not compat_dir.exists():
        return {}

    result: dict[str, CompatList] = {}
    for yaml_path in sorted(compat_dir.glob("*.yaml")):
        with open(yaml_path) as f:
            data = _yaml.safe_load(f)
        if not isinstance(data, dict):
            continue
        system = str(data.get("system", yaml_path.stem))
        cl = CompatList(
            system=system,
            chip=str(data.get("chip", chip)),
            match_by=str(data.get("match_by", "title")),
            games={str(k): str(v) for k, v in (data.get("games") or {}).items()},
        )
        result[system] = cl
    return result


def save_compat_list(compat: CompatList, output_path: Path) -> None:
    """Write a CompatList to YAML at output_path."""
    if _yaml is None:
        raise ImportError("PyYAML is required for compat list support")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "chip": compat.chip,
        "system": compat.system,
        "match_by": compat.match_by,
        "games": dict(sorted(compat.games.items())),
    }
    with open(output_path, "w") as f:
        _yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
