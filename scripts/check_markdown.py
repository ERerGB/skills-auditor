#!/usr/bin/env python3
"""Validate repository-local Markdown links and GitHub-style heading anchors."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def markdown_files() -> list[Path]:
    files = [PROJECT_ROOT / "README.md", PROJECT_ROOT / "SKILL.md"]
    for directory in ("docs", "doc", "skills"):
        root = PROJECT_ROOT / directory
        if root.is_dir():
            files.extend(root.rglob("*.md"))
    return sorted(set(path for path in files if path.is_file()))


def github_slug(label: str) -> str:
    label = re.sub(r"<[^>]+>", "", label)
    label = label.replace("`", "").strip().lower()
    kept = []
    for character in label:
        category = unicodedata.category(character)
        if character in {" ", "-", "_"} or category[0] in {"L", "N"}:
            kept.append(character)
    return "".join(kept).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING_PATTERN.match(line)
        if not match:
            continue
        base = github_slug(match.group(2))
        index = counts.get(base, 0)
        counts[base] = index + 1
        found.add(base if index == 0 else f"{base}-{index}")
    return found


def target_parts(raw: str) -> tuple[str, str]:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    path, separator, fragment = target.partition("#")
    return unquote(path), unquote(fragment) if separator else ""


def main() -> int:
    files = markdown_files()
    anchor_cache = {path.resolve(): anchors(path) for path in files}
    errors: list[str] = []

    for source in files:
        text = source.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in LINK_PATTERN.finditer(line):
                raw = match.group(1)
                if raw.startswith(("http://", "https://", "mailto:")):
                    continue
                path_text, fragment = target_parts(raw)
                target = source if not path_text else source.parent / path_text
                target = target.resolve(strict=False)
                if not target.exists():
                    errors.append(
                        f"{source.relative_to(PROJECT_ROOT)}:{line_number}: missing {raw}"
                    )
                    continue
                if fragment and target.is_file() and target.suffix.lower() == ".md":
                    target_anchors = anchor_cache.get(target)
                    if target_anchors is None:
                        target_anchors = anchors(target)
                        anchor_cache[target] = target_anchors
                    if fragment not in target_anchors:
                        errors.append(
                            f"{source.relative_to(PROJECT_ROOT)}:{line_number}: "
                            f"missing anchor #{fragment} in {target.relative_to(PROJECT_ROOT)}"
                        )

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"checked {len(files)} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
