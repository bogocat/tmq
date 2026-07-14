"""Repo registry: knows where each short name lives and how to dispatch it.

The 24-rep baseline ship at aoe_tmq/data/default_registry.json (frozen by the
plugin). A user can shadow it with a JSON file at the path set by the
``registry_path`` setting, defaulting to ``~/.config/tmq/registry.json``.

A registry entry has the shape::

    {
      "distillery": {
        "path": "/root/projects/distillery",
        "gh":   "bogocat/distillery",
        "worktree": true,
        "active": true
      },
      ...
    }

``worktree=true`` means features/chores/edits happen in an isolated git
worktree under /root/wt-<short>-<num>; worktree=false means they go in place
in the main checkout (matches the convention in the old tms/bin/tmq).

Validation rejects unknown keys, missing required fields, paths that are
empty, and a ``gh`` slug that doesn't parse as ``owner/repo``. Errors raise
``RegistryError`` so the CLI can surface them with a single message instead
of a stack trace.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

GH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RegistryError(ValueError):
    """Bad registry entry: unknown repo, missing required field, or invalid slug."""


@dataclass(frozen=True)
class RepoEntry:
    short: str
    path: str
    gh: str
    worktree: bool

    @property
    def owner(self) -> str:
        return self.gh.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.gh.split("/", 1)[1]


def _entry_from(short: str, raw: dict[str, Any]) -> RepoEntry:
    if not isinstance(raw, dict):
        raise RegistryError(f"{short!r}: entry must be an object, got {type(raw).__name__}")
    missing = {"path", "gh"} - raw.keys()
    if missing:
        raise RegistryError(f"{short!r}: missing required field(s): {', '.join(sorted(missing))}")
    path = str(raw["path"])
    gh = str(raw["gh"])
    if not path:
        raise RegistryError(f"{short!r}: path is empty")
    if not GH_RE.match(gh):
        raise RegistryError(f"{short!r}: gh slug {gh!r} does not match owner/repo shape")
    worktree = bool(raw.get("worktree", False))
    # Drop unknown keys loudly: a stale field here is almost always a typo
    # the operator wanted us to act on.
    allowed = {"path", "gh", "worktree"}
    extras = set(raw.keys()) - allowed
    if extras:
        raise RegistryError(f"{short!r}: unknown field(s): {', '.join(sorted(extras))}")
    return RepoEntry(short=short, path=path, gh=gh, worktree=worktree)


def _shipped_default() -> dict[str, RepoEntry]:
    """Read the frozen 24-rep baseline shipped with the package."""
    raw = resources.files("tmq.data").joinpath("default_registry.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RegistryError(f"shipped registry must be an object, got {type(parsed).__name__}")
    return {name: _entry_from(name, entry) for name, entry in parsed.items()}


def _user_overlay(path: str) -> dict[str, RepoEntry]:
    """Read the user-supplied overlay; missing/empty file = no overlay.

    An empty file (which the operator might create with `touch`) is treated as
    no overlay rather than a syntax error, since the deliberate fallback to
    the shipped defaults is the user-friendly path.
    """
    if not path:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        body = f.read().strip()
    if not body:
        return {}
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RegistryError(f"user registry {path!r} must be an object, got {type(parsed).__name__}")
    return {name: _entry_from(name, entry) for name, entry in parsed.items()}


def load(registry_path: str = "") -> dict[str, RepoEntry]:
    """Compose shipped defaults + user overlay. User wins on key collision."""
    repos = _shipped_default()
    repos.update(_user_overlay(registry_path))
    return repos


def get(repos: dict[str, RepoEntry], short: str) -> RepoEntry:
    """Look up by short name; raise RegistryError for unknown."""
    try:
        return repos[short]
    except KeyError as exc:
        raise RegistryError(f"unknown repo {short!r}; known: {', '.join(sorted(repos))}") from exc


def machine_summary(repos: dict[str, RepoEntry]) -> list[dict[str, Any]]:
    """Stable, machine-readable dump for `tmq list --machine`."""
    return [
        {
            "short": r.short,
            "path": r.path,
            "gh": r.gh,
            "worktree": r.worktree,
        }
        for r in (repos[k] for k in sorted(repos))
    ]
