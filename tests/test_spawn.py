"""Tests for the spawn wrapper: cmd_override construction + aoe argv.

Two regressions locked here, both of which silently broke at the live
host:

- v0.1.0 → v0.1.1: pi 0.80.x refused `pi @file` without `--approve`,
  so any dispatched agent on the new tmq has been failing since the
  pi upgrade. This test would have caught it.
- v0.1.0 only ever passed a non-empty branch to `aoe add -w`. A
  feature dispatch on a `feat/...` branch has been fine, but the
  list-spread refactor (now exercised here) makes the empty-branch
  path explicit so a future helper change can't regress it.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tmq import spawn
from tmq.spawn import SpawnRequest, _build_cmd_override
from tmq.spawn import spawn as spawn_top

# ── _build_cmd_override ────────────────────────────────────────────────


def _req(agent: str, **overrides) -> SpawnRequest:
    base = SpawnRequest(
        session_name="feat-distillery#245",
        cwd="/root/projects/distillery",
        prompt_path=Path("/tmp/tmq-prompt-feat-distillery#245.txt"),
        branch="feat/issue-245-rebase",
        agent=agent,
        worktree=True,
        cc_allow_root=False,
        pi_provider=None,
        pi_model=None,
        repo_short="distillery",
        repo_gh="bogocat/distillery",
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def test_pi_cmd_override_includes_approve_flag():
    """pi 0.80.x refuses `pi @file` without `--approve`; lock it in."""
    req = _req("pi")
    cmd = _build_cmd_override(req)
    assert "--approve" in cmd, f"pi cmd_override missing --approve: {cmd!r}"
    # Order: --approve comes between extra args and the @file reference.
    approve_idx = cmd.index("--approve")
    at_idx = cmd.index("@/tmp/")
    assert approve_idx < at_idx


def test_pi_cmd_override_passes_provider_and_model():
    req = _req("pi", pi_provider="minimax", pi_model="MiniMax-M3")
    cmd = _build_cmd_override(req)
    assert "--provider minimax" in cmd
    assert "--model MiniMax-M3" in cmd
    # Provider/model come *before* --approve (consistent argv order, easier to read).
    provider_idx = cmd.index("--provider")
    approve_idx = cmd.index("--approve")
    assert provider_idx < approve_idx


def test_pi_cmd_override_omits_provider_when_empty():
    req = _req("pi", pi_provider=None, pi_model=None)
    cmd = _build_cmd_override(req)
    assert "--provider" not in cmd
    assert "--model" not in cmd
    assert "--approve" in cmd


def test_cc_cmd_override_unchanged():
    """cc path has its own gate (root refusal) but no --approve analog yet."""
    req = _req("cc", cc_allow_root=True)
    cmd = _build_cmd_override(req)
    assert cmd.startswith("cat '/tmp/tmq-prompt-feat-distillery#245.txt'")
    assert "claude --dangerously-skip-permissions" in cmd


def test_opencode_cmd_override_unchanged():
    req = _req("oc")
    cmd = _build_cmd_override(req)
    assert cmd.startswith("cat '/tmp/tmq-prompt-feat-distillery#245.txt'")
    assert "opencode" in cmd


def test_unknown_agent_raises():
    req = _req("zz")
    with pytest.raises(spawn.SpawnError):
        _build_cmd_override(req)


# ── _modern_node_prefix ────────────────────────────────────────────────


def _mk_node(nvm_dir: Path, version: str) -> Path:
    bindir = nvm_dir / "versions" / "node" / version / "bin"
    bindir.mkdir(parents=True)
    (bindir / "node").write_text("#!/bin/sh\n")
    return bindir


def test_modern_node_prefix_picks_newest_ge_22(tmp_path, monkeypatch):
    _mk_node(tmp_path, "v20.19.2")
    _mk_node(tmp_path, "v22.15.0")
    _mk_node(tmp_path, "v22.19.0")
    monkeypatch.setenv("NVM_DIR", str(tmp_path))
    assert spawn._modern_node_prefix() == (
        f"PATH='{tmp_path}/versions/node/v22.19.0/bin':\"$PATH\" "
    )


def test_modern_node_prefix_ignores_below_22(tmp_path, monkeypatch):
    _mk_node(tmp_path, "v18.20.4")
    _mk_node(tmp_path, "v20.19.2")
    monkeypatch.setenv("NVM_DIR", str(tmp_path))
    assert spawn._modern_node_prefix() == ""


def test_modern_node_prefix_handles_future_majors(tmp_path, monkeypatch):
    _mk_node(tmp_path, "v22.19.0")
    _mk_node(tmp_path, "v24.1.0")
    monkeypatch.setenv("NVM_DIR", str(tmp_path))
    assert "v24.1.0" in spawn._modern_node_prefix()


def test_modern_node_prefix_missing_nvm_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NVM_DIR", str(tmp_path / "nonexistent"))
    assert spawn._modern_node_prefix() == ""


def test_pi_cmd_override_starts_with_node_prefix(monkeypatch):
    monkeypatch.setattr(
        spawn, "_modern_node_prefix",
        lambda: "PATH='/x/y/bin':\"$PATH\" ",
    )
    cmd = _build_cmd_override(_req("pi"))
    assert cmd.startswith("PATH='/x/y/bin':\"$PATH\" PI_DISPATCH_AUTOAPPROVE=1 pi")


# ── _spawn_via_aoe argv ──────────────────────────────────────────────


def test_spawn_via_aoe_omits_w_flag_when_branch_none():
    """A `branch=None` (in-place chore) must not render `-w ''`."""
    req = _req("pi", branch=None)
    argv = _captured_aoe_argv(req)
    assert "-w" not in argv
    assert "" not in argv  # empty-string sentinel from `-w ''`


def test_spawn_via_aoe_includes_w_with_branch():
    req = _req("pi", branch="feat/issue-245-rebase")
    argv = _captured_aoe_argv(req)
    w_idx = argv.index("-w")
    assert argv[w_idx + 1] == "feat/issue-245-rebase"


def test_spawn_via_aoe_purges_before_add():
    """`aoe rm --purge` must fire before `aoe add` so re-dispatch picks up
    the new cmd-override even when the prior session was removed without --purge."""
    req = _req("pi", branch=None)
    call_sequence = _captured_aoe_call_sequence(req)
    cmds = [c[:3] if len(c) >= 3 else c for c in call_sequence]
    # First call must be aoe rm --purge
    assert cmds[0] == ["aoe", "rm", "--purge"], f"expected aoe rm --purge first, got: {cmds}"
    # Second call must be aoe add
    assert cmds[1] == ["aoe", "add", req.cwd], f"expected aoe add second, got: {cmds}"


def test_spawn_via_aoe_includes_cmd_override_with_approve():
    """End-to-end: the full aoe add argv carries a --approve-bearing cmd_override."""
    req = _req("pi")
    argv = _captured_aoe_argv(req)
    co_idx = argv.index("--cmd-override")
    cmd = argv[co_idx + 1]
    assert "--approve" in cmd


def _captured_aoe_call_sequence(req: SpawnRequest) -> list[list[str]]:
    """Drive spawn.spawn() with all subprocess calls mocked, capture the
    full sequence of `_run` argv calls (rm, add, session start, etc.).
    """
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(list(args))
        m = mock.Mock()
        m.returncode = 0
        m.stdout = '{"id": "abcdef1234567890"}'
        m.stderr = ""
        return m

    with mock.patch.object(spawn.shutil, "which", return_value="/usr/local/bin/aoe"):
        with mock.patch.object(spawn, "_run", side_effect=fake_run):
            with mock.patch.object(spawn, "prompt_path_for", return_value=req.prompt_path):
                try:
                    spawn_top(req)
                except spawn.SpawnError:
                    pass
    return captured


def _captured_aoe_argv(req: SpawnRequest) -> list[str]:
    """Drive spawn.spawn() with all subprocess calls mocked, capture the
    `aoe add` argv. Returns the empty list when aoe is missing -- the
    test caller asserts shape from the captured argv directly.
    """
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        if args and args[0] == "aoe" and args[1] == "add":
            captured.append(list(args))
            # Return a fake CompletedProcess-like object.
            m = mock.Mock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m
        # aoe session start + aoe session show stubs.
        m = mock.Mock()
        m.returncode = 0
        m.stdout = '{"id": "abcdef1234567890"}'
        m.stderr = ""
        return m

    with mock.patch.object(spawn.shutil, "which", return_value="/usr/local/bin/aoe"):
        with mock.patch.object(spawn, "_run", side_effect=fake_run):
            with mock.patch.object(spawn, "prompt_path_for", return_value=req.prompt_path):
                try:
                    spawn_top(req)
                except spawn.SpawnError:
                    pass  # ignore cleanup-path failures; we only care about argv
    assert captured, "aoe add was not invoked"
    return captured[0]
