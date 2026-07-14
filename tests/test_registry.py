"""Tests for the repo registry — JSON loading + validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tmq.registry import RegistryError, get, load, machine_summary

REPO_ENTRY_GH = "bogocat/distillery"


def test_loads_default_registry():
    repos = load()
    # 20 from the bash tool + the tmq self-reference = 21 baseline (we
    # added `tmq` so the plugin can dispatch to its own repo).
    assert "distillery" in repos
    assert "home-portal" in repos
    assert repos["distillery"].path == "/root/projects/distillery"
    assert repos["distillery"].gh == REPO_ENTRY_GH
    assert repos["distillery"].worktree is True


def test_short_lookup():
    repos = load()
    entry = get(repos, "distillery")
    assert entry.short == "distillery"
    assert entry.owner == "bogocat"
    assert entry.repo == "distillery"


def test_unknown_repo_raises():
    repos = load()
    with pytest.raises(RegistryError) as excinfo:
        get(repos, "no-such-repo")
    assert "no-such-repo" in str(excinfo.value)


def test_user_overlay_shadows_default(tmp_path: Path):
    overlay = tmp_path / "registry.json"
    overlay.write_text(
        json.dumps(
            {
                "distillery": {
                    "path": "/custom/distillery",
                    "gh": "someoneelse/distillery",
                    "worktree": False,
                },
            }
        ),
        encoding="utf-8",
    )
    repos = load(str(overlay))
    assert repos["distillery"].path == "/custom/distillery"
    assert repos["distillery"].gh == "someoneelse/distillery"
    assert repos["distillery"].worktree is False
    # Other repos still ship.
    assert "home-portal" in repos
    assert repos["home-portal"].path == "/root/projects/home-portal"


def test_empty_user_overlay_is_noop(tmp_path: Path):
    overlay = tmp_path / "empty.json"
    overlay.write_text("", encoding="utf-8")
    repos = load(str(overlay))
    assert repos["distillery"].path == "/root/projects/distillery"


def test_user_overlay_with_invalid_entry_raises(tmp_path: Path):
    overlay = tmp_path / "bad.json"
    overlay.write_text(json.dumps({"foo": {"path": "/x"}}), encoding="utf-8")
    with pytest.raises(RegistryError) as excinfo:
        load(str(overlay))
    assert "foo" in str(excinfo.value)
    assert "gh" in str(excinfo.value)


def test_unknown_field_is_rejected():
    import tmq.registry as reg

    with pytest.raises(RegistryError) as excinfo:
        reg._entry_from("x", {"path": "/p", "gh": "o/r", "active": True})
    assert "active" in str(excinfo.value)


def test_machine_summary_shape():
    repos = load()
    summary = machine_summary(repos)
    assert isinstance(summary, list)
    first = summary[0]
    assert set(first.keys()) == {"short", "path", "gh", "worktree"}
