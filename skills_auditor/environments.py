"""Native skill-host environment descriptors.

Keep host path conventions out of discovery and lifecycle algorithms. A third-party
host can participate by supplying another ``NativeEnvironment`` descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Dict, Iterable, Tuple


@dataclass(frozen=True)
class NativeEnvironment:
    key: str
    project_skill_roots: Tuple[str, ...]
    global_skill_roots: Tuple[str, ...]
    global_home_variable: str = ""

    def project_roots(self, project_root: Path) -> Tuple[Path, ...]:
        return tuple(project_root / relative for relative in self.project_skill_roots)

    def global_roots(self, home: Path) -> Tuple[Path, ...]:
        if self.global_home_variable and os.environ.get(self.global_home_variable):
            custom = Path(os.environ[self.global_home_variable]).expanduser()
            if not custom.is_absolute():
                raise ValueError(f"{self.global_home_variable} must be an absolute owner-local path")
            return (custom / "skills", *(home / relative for relative in self.global_skill_roots[1:]))
        return tuple(home / relative for relative in self.global_skill_roots)

    def primary_project_root(self, project_root: Path) -> Path:
        if not self.project_skill_roots:
            raise ValueError(f"Environment {self.key!r} has no project skill root")
        return self.project_roots(project_root)[0]

    def primary_global_root(self, home: Path) -> Path:
        if not self.global_skill_roots:
            raise ValueError(f"Environment {self.key!r} has no global skill root")
        return self.global_roots(home)[0]


class NativeEnvironmentRegistry:
    def __init__(self, environments: Iterable[NativeEnvironment] = ()) -> None:
        self._environments: Dict[str, NativeEnvironment] = {}
        for environment in environments:
            self.register(environment)

    def register(self, environment: NativeEnvironment) -> None:
        if not environment.key.strip():
            raise ValueError("Environment key must not be empty")
        if environment.key in self._environments:
            raise ValueError(f"Environment already registered: {environment.key}")
        self._environments[environment.key] = environment

    def get(self, key: str) -> NativeEnvironment:
        try:
            return self._environments[key]
        except KeyError as exc:
            known = ", ".join(sorted(self._environments))
            raise ValueError(f"Unknown environment {key!r}; known: {known}") from exc

    def all(self) -> Tuple[NativeEnvironment, ...]:
        return tuple(self._environments.values())


BUILTIN_ENVIRONMENTS = NativeEnvironmentRegistry(
    (
        NativeEnvironment("cursor", (".cursor/skills",), (".cursor/skills",)),
        NativeEnvironment("claude-code", (".claude/skills",), (".claude/skills",)),
        NativeEnvironment("codex", (".agents/skills", ".codex/skills"),
                          (".agents/skills", ".codex/skills")),
        NativeEnvironment("grok", (".grok/skills",), (".grok/skills",), "GROK_HOME"),
    )
)


def builtin_project_skill_roots(project_root: Path) -> Tuple[Path, ...]:
    return tuple(
        root
        for environment in BUILTIN_ENVIRONMENTS.all()
        for root in environment.project_roots(project_root)
    )
