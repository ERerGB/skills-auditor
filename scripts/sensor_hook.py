#!/usr/bin/env python3
"""Compatibility launcher for the skill-trace plugin hook.

Codex executes hook commands from the active workspace directory. This root
launcher gives old and new hook registrations a stable entrypoint and never
blocks Codex when sensor capture fails.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    target = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "skill-trace"
        / "scripts"
        / "sensor_hook.py"
    )
    if not target.is_file():
        print(f"skill-trace launcher could not find {target}", file=sys.stderr)
        return 0
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            return 0
        print(f"skill-trace launcher swallowed hook failure: exit {code}", file=sys.stderr)
        return 0
    except BaseException as exc:
        print(f"skill-trace launcher swallowed hook failure: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
