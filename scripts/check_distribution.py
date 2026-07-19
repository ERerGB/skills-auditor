#!/usr/bin/env python3
"""Fail when built distributions omit the public integration contract."""

from __future__ import annotations

import argparse
import runpy
import tarfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = runpy.run_path(str(PROJECT_ROOT / "skills_auditor" / "_version.py"))["__version__"]


PACKAGE_FILES = {
    "skills_auditor/__init__.py",
    "skills_auditor/__main__.py",
    "skills_auditor/_version.py",
    "skills_auditor/cli.py",
    "skills_auditor/environments.py",
    "skills_auditor/integration.py",
    "skills_auditor/ledger.py",
    "skills_auditor/observability.py",
    "skills_auditor/state_machine.py",
    "skills_auditor/schemas/error-v1.schema.json",
    "skills_auditor/schemas/integration-plan-v1.schema.json",
    "skills_auditor/schemas/integration-receipt-v1.schema.json",
    "skills_auditor/schemas/integration-spec-v1.schema.json",
    "skills_auditor/schemas/integration-verification-v1.schema.json",
}


def only_match(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"expected one {pattern!r} in {root}, found {len(matches)}")
    return matches[0]


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = PACKAGE_FILES - names
        if missing:
            raise SystemExit(f"wheel is missing: {sorted(missing)}")
        metadata_name = only_name(names, ".dist-info/METADATA")
        entry_points_name = only_name(names, ".dist-info/entry_points.txt")
        license_name = only_name(names, ".dist-info/licenses/LICENSE")
        metadata = archive.read(metadata_name).decode("utf-8")
        entry_points = archive.read(entry_points_name).decode("utf-8")
        archive.read(license_name)

    required_metadata = {
        f"Version: {VERSION}",
        "License-Expression: MIT",
        "License-File: LICENSE",
    }
    for line in required_metadata:
        if line not in metadata:
            raise SystemExit(f"wheel metadata is missing {line!r}")
    if "skills-audit = skills_auditor.cli:main" not in entry_points:
        raise SystemExit("wheel entry points do not expose skills-audit")


def only_name(names: set[str], suffix: str) -> str:
    matches = sorted(name for name in names if name.endswith(suffix))
    if len(matches) != 1:
        raise SystemExit(f"expected one archive member ending in {suffix!r}")
    return matches[0]


def check_sdist(path: Path) -> None:
    prefix = f"skills_auditor-{VERSION}/"
    required = {prefix + name for name in PACKAGE_FILES}
    required.update(
        {
            prefix + "LICENSE",
            prefix + "README.md",
            prefix + "SKILL.md",
            prefix + "config/skills-auditor.integration.example.json",
            prefix + "docs/integration-contract.md",
            prefix + "docs/releasing.md",
            prefix + "scripts/check_distribution.py",
            prefix + "scripts/check_markdown.py",
            prefix + "tests/test_integration.py",
        }
    )
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
    missing = required - names
    if missing:
        raise SystemExit(f"source distribution is missing: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    wheel = only_match(args.dist, "skills_auditor-*.whl")
    sdist = only_match(args.dist, "skills_auditor-*.tar.gz")
    check_wheel(wheel)
    check_sdist(sdist)
    print(f"checked {wheel.name} and {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
