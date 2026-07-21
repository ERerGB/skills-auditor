#!/usr/bin/env python3
"""Install one wheel into a clean venv and run an artifact test layer.

The suite is copied outside the checkout before execution. This prevents the
repository's source tree from satisfying imports that should come from the wheel.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAYERS = {
    "smoke": PROJECT_ROOT / "smoke_tests",
    "e2e": PROJECT_ROOT / "e2e_tests",
}


def resolve_wheel(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_file() and path.suffix == ".whl":
        return path
    if path.is_dir():
        wheels = sorted(path.glob("skills_auditor-*.whl"))
        if len(wheels) == 1:
            return wheels[0].resolve()
        raise SystemExit(f"expected exactly one skills_auditor wheel in {path}, found {len(wheels)}")
    raise SystemExit(f"wheel path does not exist or is not a .whl file: {path}")


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit(
                f"expected exactly one .dist-info/METADATA in {wheel}, found {len(metadata_names)}"
            )
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    version = metadata.get("Version", "").strip()
    if not version:
        raise SystemExit(f"wheel metadata has no Version field: {wheel}")
    return version


def environment_paths(environment: Path) -> tuple[Path, Path, Path]:
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    cli = scripts / ("skills-audit.exe" if os.name == "nt" else "skills-audit")
    pip = scripts / ("pip.exe" if os.name == "nt" else "pip")
    return python, cli, pip


def checked_run(command: Sequence[str], *, cwd: Path, environment: dict[str, str]) -> None:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def run_layer(layer: str, wheel: Path) -> None:
    source_suite = LAYERS[layer]
    if not source_suite.is_dir():
        raise SystemExit(f"artifact test layer does not exist: {source_suite}")

    with tempfile.TemporaryDirectory(prefix=f"skills-auditor-{layer}-") as base:
        root = Path(base)
        environment_root = root / "venv"
        suite = root / "suite"
        shutil.copytree(source_suite, suite)
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_root)
        python, cli, pip = environment_paths(environment_root)

        clean_environment = os.environ.copy()
        clean_environment.pop("PYTHONHOME", None)
        clean_environment.pop("PYTHONPATH", None)
        clean_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        checked_run(
            [str(pip), "install", "--no-deps", str(wheel)],
            cwd=root,
            environment=clean_environment,
        )
        if not cli.is_file():
            raise SystemExit(f"installed wheel did not create the console entry point: {cli}")

        test_environment = clean_environment.copy()
        test_environment.update(
            {
                "SKILLS_AUDITOR_CLI": str(cli),
                "SKILLS_AUDITOR_PYTHON": str(python),
                "SKILLS_AUDITOR_PROJECT_ROOT": str(PROJECT_ROOT),
                "SKILLS_AUDITOR_EXPECTED_VERSION": wheel_version(wheel),
            }
        )
        checked_run(
            [
                str(python),
                "-m",
                "unittest",
                "discover",
                "-s",
                str(suite),
                "-p",
                "test_*.py",
                "-v",
            ],
            cwd=root,
            environment=test_environment,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=sorted(LAYERS))
    parser.add_argument("wheel", type=Path, help="Wheel file or directory containing exactly one wheel")
    args = parser.parse_args()
    wheel = resolve_wheel(args.wheel)
    print(f"artifact layer: {args.layer}; wheel: {wheel.name}", flush=True)
    run_layer(args.layer, wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
