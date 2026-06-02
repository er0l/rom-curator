"""Validate that ZIP archives contain files matching the expected system extensions.

Each system in ``mappings/systems.yaml`` can declare an ``extensions`` list.
For every ZIP file found in that system's NAS folder this tool peeks inside and
flags any file whose extension belongs to a *different* known system — e.g.
a ``.gbc`` file hiding inside ``gb/``.

Extensions that are generic or shared across many systems (e.g. ``.bin``,
``.iso``) are only flagged if the inner extension is exclusively claimed by
another system.

Usage::

    # Check all systems that have an extensions list
    python3 romcurator.py zip-check

    # Check specific systems only
    python3 romcurator.py zip-check --systems gb,gbc,gba

    # Show OK systems as well (default hides them)
    python3 romcurator.py zip-check --verbose
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from rich.console import Console
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ZipMismatch:
    """One ZIP file that contains at least one wrong-system file."""
    zip_path: Path
    system: str
    # ext → list[suggested_system]
    bad_exts: dict[str, list[str]] = field(default_factory=dict)

    @property
    def relative_path(self) -> Path:
        return self.zip_path

    def add(self, ext: str, suggested: list[str]) -> None:
        if ext not in self.bad_exts:
            self.bad_exts[ext] = suggested


@dataclass
class SystemCheckResult:
    canonical: str
    zips_checked: int = 0
    mismatches: list[ZipMismatch] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


@dataclass
class ZipCheckSummary:
    systems_checked: int = 0
    zips_checked: int = 0
    mismatch_count: int = 0
    results: list[SystemCheckResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_zip_check(
    config: dict[str, object],
    mappings: dict[str, dict[str, object]],
    *,
    systems: list[str] | None = None,
    verbose: bool = False,
) -> ZipCheckSummary:
    """Check all (or selected) system ZIP archives for wrong-system content.

    Parameters
    ----------
    config:
        Curator config dict (paths.roms must be set).
    mappings:
        Loaded system mappings dict from systems.yaml.
    systems:
        If given, only check these canonical system names.
    verbose:
        If True, also print systems with no mismatches.
    """
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Config key 'paths' must be a mapping")
    roms_root = Path(str(paths.get("roms", "/mnt/storage/roms"))).expanduser()

    console = Console() if Console else None

    # Build extension → [canonical_systems] index from mappings
    ext_to_systems: dict[str, list[str]] = {}
    sys_to_exts: dict[str, set[str]] = {}

    for canonical, meta in mappings.items():
        if not isinstance(meta, dict):
            continue
        exts = meta.get("extensions")
        if not exts or not isinstance(exts, list):
            continue
        sys_to_exts[canonical] = {str(e).lower().lstrip(".") for e in exts}
        for ext in sys_to_exts[canonical]:
            ext_to_systems.setdefault(ext, []).append(canonical)

    # Determine which systems to check
    if systems:
        check_systems = [s for s in systems if s in sys_to_exts]
        unknown = [s for s in systems if s not in sys_to_exts]
        if unknown:
            _print(
                console,
                f"[yellow]No extension rules configured for: {', '.join(unknown)} — skipping[/yellow]",
            )
    else:
        check_systems = sorted(sys_to_exts.keys())

    summary = ZipCheckSummary()

    for canonical in check_systems:
        meta = mappings.get(canonical, {})
        nas_path = str(meta.get("nas", canonical)) if isinstance(meta, dict) else canonical
        system_dir = roms_root / nas_path
        if not system_dir.is_dir():
            continue

        allowed_exts = sys_to_exts[canonical]
        result = SystemCheckResult(canonical=canonical)
        summary.systems_checked += 1

        # Walk all .zip files recursively under the system folder
        for zip_path in sorted(system_dir.rglob("*.zip")):
            mismatch: ZipMismatch | None = None
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for name in zf.namelist():
                        if name.endswith("/"):
                            continue  # directory entry
                        inner_ext = Path(name).suffix.lower().lstrip(".")
                        if not inner_ext:
                            continue
                        # Only flag extensions that unambiguously belong to another system
                        if inner_ext in allowed_exts:
                            continue
                        other_systems = [
                            s for s in ext_to_systems.get(inner_ext, [])
                            if s != canonical
                        ]
                        if not other_systems:
                            continue  # unknown / generic extension — skip
                        if mismatch is None:
                            mismatch = ZipMismatch(zip_path=zip_path, system=canonical)
                        mismatch.add(inner_ext, other_systems)
                result.zips_checked += 1
                summary.zips_checked += 1
            except (zipfile.BadZipFile, OSError):
                result.zips_checked += 1
                summary.zips_checked += 1
                continue

            if mismatch is not None:
                result.mismatches.append(mismatch)

        summary.results.append(result)
        if result.mismatches:
            summary.mismatch_count += len(result.mismatches)
        _print_system_result(console, result, roms_root, verbose=verbose)

    _print_summary(console, summary)
    return summary


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_system_result(
    console,
    result: SystemCheckResult,
    roms_root: Path,
    *,
    verbose: bool,
) -> None:
    if result.ok:
        if verbose:
            _print(
                console,
                f"  [green]✓[/green] [bold]{result.canonical}[/bold]"
                f" — {result.zips_checked} ZIP(s) OK",
            )
        return

    _print(
        console,
        f"\n[bold yellow]{result.canonical}[/bold yellow]"
        f" — {len(result.mismatches)} ZIP(s) with wrong-system content:",
    )
    for m in result.mismatches:
        try:
            rel = m.zip_path.relative_to(roms_root)
        except ValueError:
            rel = m.zip_path
        ext_hints = ", ".join(
            f".{ext} → [italic]{'/'.join(syss)}[/italic]"
            for ext, syss in sorted(m.bad_exts.items())
        )
        _print(console, f"  {rel}  [dim][{ext_hints}][/dim]")


def _print_summary(console, summary: ZipCheckSummary) -> None:
    _print(
        console,
        f"\nChecked [bold]{summary.systems_checked}[/bold] system(s), "
        f"[bold]{summary.zips_checked}[/bold] ZIP file(s).",
    )
    if summary.mismatch_count:
        _print(
            console,
            f"[bold red]{summary.mismatch_count} ZIP(s) contain wrong-system files.[/bold red]",
        )
    else:
        _print(console, "[bold green]No mismatches found.[/bold green]")


def _print(console, msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style) if style else console.print(msg)
    else:
        plain = re.sub(r"\[/?[^\[\]]*\]", "", msg)
        print(plain)
