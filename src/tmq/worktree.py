"""Worktree management — git worktree add/find/clean with feature-branch naming.

Layout matches tms/bin/tmq's `create_worktree` function:

- Feature work in a worktree-enabled repo lives in ``/root/wt-<short>-<num>``
  on a feature branch ``feat/issue-<num>-<slug>``.
- Fix/chore on a worktree-enabled repo still goes in main via the main
  checkout (no isolation) because bash treats them as single-line edits.
- All work in a worktree-disabled repo goes in the main checkout, full stop.

Branch slug is a 30-char title-derived kebab. If a feature branch already
exists from a previous dispatch we reuse it instead of failing the dispatch
(this matches the bash `git checkout -B` behavior at create_worktree:653-660).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tmq import gh as gh_helper
from tmq.registry import RepoEntry

WORKTREE_ROOT = "/root"
SLUG_MAX = 30  # branch slug length cap; matches the bash sed at line ~720


class WorktreeError(RuntimeError):
    """A git worktree command failed or the resulting state is unsafe."""


@dataclass(frozen=True)
class WorktreeResult:
    cwd: str
    branch: str

    @property
    def is_worktree(self) -> bool:
        # The convention "/root/wt-<short>-<num>" puts every isolated worktree
        # under WORKTREE_ROOT. The main checkout (or any other path outside
        # /root/wt-*) is treated as in-place. Used by the prompt template to
        # decide whether to suggest "cd $cwd" before editing.
        return self.cwd.startswith(f"{WORKTREE_ROOT}/wt-")


def _run(args: list[str], *, cwd: str, check: bool = True) -> str:
    """Wrap `subprocess.run` with the same error semantics as `git` failures.

    A failed `git` command raises WorktreeError carrying the captured
    stderr so the operator gets a usable diagnostic, not a stack trace from
    a bare CalledProcessError.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=20.0,
        )
    except FileNotFoundError as exc:
        raise WorktreeError(f"{args[0]!r} not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"`{' '.join(args)}` timed out in {cwd}") from exc
    if check and proc.returncode != 0:
        snippet = proc.stderr.strip().splitlines()[:3] or ["(no stderr)"]
        raise WorktreeError(f"`{' '.join(args)}` failed in {cwd}: {' | '.join(snippet)}")
    return (proc.stdout or "").strip()


def _main_checkout(repo: RepoEntry) -> str:
    return repo.path


def _feature_branch_name(issue_number: int, title: str) -> str:
    slug = gh_helper.slugify(title)[:SLUG_MAX].rstrip("-")
    return f"feat/issue-{issue_number}-{slug}" if slug else f"feat/issue-{issue_number}"


def _worktree_path(short: str, issue_number: int) -> str:
    return f"{WORKTREE_ROOT}/wt-{short}-{issue_number}"


def _existing_feat_branch(repo: RepoEntry, branch_name: str) -> str | None:
    """Return the worktree path that already has branch_name checked out, or None."""
    out = _run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=_main_checkout(repo),
    )
    # Porcelain format:
    #   worktree /root/wt-distillery-245
    #   HEAD 1a2b3c4...
    #   branch refs/heads/feat/issue-245-rebase-tooling
    worktrees: dict[str, str] = {}
    current_path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
            worktrees.setdefault(current_path, "")
        elif line.startswith("branch ") and current_path:
            worktrees[current_path] = line[len("branch ") :]
    for wt_path, branch in worktrees.items():
        if branch == f"refs/heads/{branch_name}":
            return wt_path
    return None


def _git_remote_main(repo: RepoEntry) -> str:
    """Return the canonical 'main' (or 'master') branch via `git symbolic-ref`.

    Defaults to 'main' if the call fails (matches the bash default at line
    ~620). Older repos that stay on master for historical reasons still
    work because git symbolic-ref can be configured to that name.
    """
    try:
        ref = _run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=_main_checkout(repo),
        )
        return ref.rsplit("/", 1)[-1] or "main"
    except WorktreeError:
        return "main"


def create(repo: RepoEntry, issue_number: int, title: str, issue_type: str) -> WorktreeResult:
    """Allocate a working tree for the dispatch.

    Returns ``WorktreeResult(cwd, branch)``: the cwd the agent should be
    launched in, and the branch it should check out (or that the new
    worktree will create).

    Feature work in a worktree-enabled repo: ``git worktree add`` an isolated
    working copy. Reuse if the branch is already checked out elsewhere.
    Everything else: use the main checkout on ``main`` (``HEAD``).

    The bash code distinguishes four issue_types. We honor that:
    - ``feature``: worktree + feat/ branch
    - ``fix``/``chore``: in-place on main, no branch name returned
    - ``review``: in-place on the PR's head ref is the correct behavior; the
      caller (handlers.review) passes us the PR's head_ref instead of a
      title slug, so the worktree code doesn't actually fire.
    """
    if not os.path.isdir(repo.path):
        raise WorktreeError(f"repo path {repo.path!r} is not a directory")

    main_branch = _git_remote_main(repo)
    if issue_type in {"fix", "chore"}:
        # In-place on main — used for quick fixes and chores. The agent is
        # expected to commit + push via the dispatch-loop prompt.
        return WorktreeResult(cwd=repo.path, branch=main_branch)

    # Feature / review path: worktree when the policy says yes, in-place
    # otherwise. Review pass-through reuses the PR's checked-out workdir if
    # the calling agent already has it; otherwise creates one.
    if repo.worktree and issue_type == "feature":
        branch = _feature_branch_name(issue_number, title)
        existing = _existing_feat_branch(repo, branch)
        if existing is not None:
            return WorktreeResult(cwd=existing, branch=branch)
        wt_path = _worktree_path(repo.short, issue_number)
        # Idempotent: git worktree add errors out if the worktree dir
        # already exists; route that through the reuse path.
        if os.path.isdir(wt_path):
            return WorktreeResult(cwd=wt_path, branch=branch)
        Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(
                ["git", "worktree", "add", "-B", branch, wt_path, main_branch],
                cwd=repo.path,
            )
        except WorktreeError:
            # Fall through to in-place if the worktree add is genuinely
            # broken (e.g. dirty main, missing remote). The agent prompt
            # explicitly tells it the cwd + branch so a fallback path is
            # tolerable; never silently drop the dispatch.
            gh_helper.warn(f"worktree add failed; falling back to main checkout at {repo.path}")
            return WorktreeResult(cwd=repo.path, branch=main_branch)
        return WorktreeResult(cwd=wt_path, branch=branch)

    # review in a non-worktree repo, or anything else: in-place.
    return WorktreeResult(cwd=repo.path, branch=main_branch)


def head_branch(repo: RepoEntry, pr_head_ref: str) -> WorktreeResult:
    """Return a working tree checked out at the PR's head_ref.

    Used by `tmq review` only: the agent should edit on the PR branch, not
    main. If the PR branch is already checked out in a worktree, reuse that
    worktree; otherwise the agent will need to run a fresh `git checkout`
    inside the main checkout (which we don't do automatically because the
    bash tool didn't either — it'd risk clobbering the operator's session).
    """
    out = _run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo.path,
    )
    worktrees: dict[str, str] = {}
    current_path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
            worktrees.setdefault(current_path, "")
        elif line.startswith("branch ") and current_path:
            worktrees[current_path] = line[len("branch ") :]
    for wt_path, branch in worktrees.items():
        if branch == f"refs/heads/{pr_head_ref}":
            return WorktreeResult(cwd=wt_path, branch=pr_head_ref)
    return WorktreeResult(cwd=repo.path, branch=pr_head_ref)
