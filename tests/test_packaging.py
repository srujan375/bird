"""Packaging: every runtime asset must actually ship.

This exists because `package-data` once pointed at `harness/instructions.md`,
a path that stopped existing when `harness/` became `engine/`. Wheels then
shipped no system prompts and no arch page, and nobody noticed — an editable
install imports straight from the source tree, so the only broken thing was
the artifact nobody tested.

These tests read pyproject.toml and assert the declared globs still match real
files, which is exactly the failure mode that went unseen.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def package_data() -> dict[str, list[str]]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cfg["tool"]["setuptools"]["package-data"]


def test_every_declared_glob_matches_something():
    """A declared pattern that matches nothing is the bug this file is about."""
    for package, patterns in package_data().items():
        pkg_dir = SRC / package.replace(".", "/")
        assert pkg_dir.is_dir(), f"package-data names {package!r}, which is not a directory"
        for pattern in patterns:
            assert list(pkg_dir.glob(pattern)), (
                f"package-data pattern {package}:{pattern!r} matches no files — "
                "it is stale, and a wheel built now would silently omit it"
            )


@pytest.mark.parametrize("relpath", [
    "harnesses/arch/instructions.md",
    "harnesses/code/instructions.md",
    "harnesses/lead/instructions.md",
    "harnesses/arch/static/index.html",
    "skills/builtin/skill-creator.md",
])
def test_runtime_asset_is_declared(relpath: str):
    """Each non-.py file the tool reads at runtime must be covered by a glob."""
    path = SRC / "ox" / relpath
    assert path.is_file(), f"{relpath} is missing from the source tree"
    covered = False
    for package, patterns in package_data().items():
        pkg_dir = SRC / package.replace(".", "/")
        for pattern in patterns:
            if path in set(pkg_dir.glob(pattern)):
                covered = True
    assert covered, (
        f"{relpath} exists but no package-data pattern covers it — "
        "it would be dropped from a wheel"
    )


def test_workbench_bundle_is_built_and_referenced():
    """The page is a build artifact (source in arch-ui/). If the built assets
    are missing or index.html points at stale filenames, `ox arch` serves a
    blank page — so the built output has to be committed, not just built."""
    static = SRC / "ox" / "harnesses" / "arch" / "static"
    index = static / "index.html"
    assert index.is_file(), "static/index.html missing — run `npm run build` in arch-ui/"
    assets = list((static / "assets").glob("*"))
    assert assets, "static/assets is empty — the Workbench bundle was never built"
    html = index.read_text(encoding="utf-8")
    for asset in assets:
        assert asset.name in html, (
            f"{asset.name} is not referenced by index.html — the committed build "
            "output is inconsistent; rebuild arch-ui and commit both together"
        )
