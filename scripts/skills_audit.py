#!/usr/bin/env python3
"""Legacy launcher: run from repo without installing the package.

Prefer after install: ``skills-audit`` or ``python -m skills_auditor``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skills_auditor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
