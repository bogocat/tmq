"""Tests for the worktree allocation logic.

The worker module shells `git` for its real work; here we exercise the
allocation algorithm by calling it against a fixture-style repo
constructed in-memory. We mock `_run` so the tests stay hermetic and
don't depend on the host's git history.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tmq import worktree as wt
from tmq.registry import RepoEntry


@pytest.fixture
def repo_entry(tmp_path) -> RepoEntry:
    (tmp_path / ".git").mkdir()
    return RepoEntry(short="distillery", path=str(tmp_path), gh="bogocat/distillery", worktree=True)


def test_feature_in_worktree_repo_returns_worktree_path(repo_entry, tmp_path):
    # Configure: pretend the repo's main branch is "main".
    fake = {
        ("git", "symbolic-ref", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("git", "worktree", "list", "--porcelain"): "(empty)",
    }
    with mock.patch.object(wt, "_run", side_effect=lambda a, **kw: fake.get(tuple(a), "")):
        result = wt.create(repo_entry, 245, "Rebase tooling", issue_type="feature")
    assert result.cwd == "/root/wt-distillery-245"
    assert result.branch == "feat/issue-245-rebase-tooling"
    assert result.is_worktree


def test_feature_branch_reuses_existing_worktree(repo_entry):
    fake = {
        ("git", "symbolic-ref", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("git", "worktree", "list", "--porcelain"): (
            "worktree /root/wt-distillery-245\nHEAD abc123\nbranch refs/heads/feat/issue-245-rebase-tooling\n"
        ),
    }
    with mock.patch.object(wt, "_run", side_effect=lambda a, **kw: fake.get(tuple(a), "")):
        result = wt.create(repo_entry, 245, "Rebase tooling", issue_type="feature")
    assert result.cwd == "/root/wt-distillery-245"
    assert result.branch == "feat/issue-245-rebase-tooling"


def test_fix_in_worktree_repo_uses_main_checkout(repo_entry):
    fake = {
        ("git", "symbolic-ref", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
    }
    with mock.patch.object(wt, "_run", side_effect=lambda a, **kw: fake.get(tuple(a), "")):
        result = wt.create(repo_entry, 245, "Bug", issue_type="fix")
    assert result.cwd == repo_entry.path
    assert result.branch == "main"
    assert not result.is_worktree


def test_feature_in_non_worktree_repo_uses_main_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    repo = RepoEntry(short="palimpsest", path=str(tmp_path), gh="bogocat/palimpsest", worktree=False)
    fake = {
        ("git", "symbolic-ref", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
    }
    with mock.patch.object(wt, "_run", side_effect=lambda a, **kw: fake.get(tuple(a), "")):
        result = wt.create(repo, 245, "Anything", issue_type="feature")
    assert result.cwd == str(tmp_path)
    assert not result.is_worktree


def test_worktree_create_falls_back_when_add_fails(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    repo = RepoEntry(short="distillery", path=str(tmp_path), gh="bogocat/distillery", worktree=True)

    def runner(args, **kw):
        if args[:3] == ["git", "worktree", "add"]:
            raise wt.WorktreeError("dirty index")
        if args[:2] == ["git", "symbolic-ref"]:
            return "refs/remotes/origin/main"
        return ""

    with mock.patch.object(wt, "_run", side_effect=runner):
        result = wt.create(repo, 245, "Anything", issue_type="feature")
    assert result.cwd == str(tmp_path)
    assert result.branch == "main"


def test_slugify_kebab_and_cap():
    assert wt._feature_branch_name(245, "Rebase the tooling!") == "feat/issue-245-rebase-the-tooling"
    long_title = "x" * 60
    br = wt._feature_branch_name(1, long_title)
    assert br.startswith("feat/issue-1-")
    assert len(br) <= len("feat/issue-1-") + 30
