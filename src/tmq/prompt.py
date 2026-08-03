"""Prompt construction -- the message body we hand to the agent (pi/cc/oc).

Mirrors the bash `build_prompt` function in tms/bin/tmq (lines 212-372):

- title
- AC restate (we pull it from the issue/PR body; the agent rewrites it
  before it starts, but spelling it out here is the cheap way to keep a
  human-readable audit trail in the dispatch event log)
- AGENTS.md clipboard contract (the marker-grammar the dispatched agent
  must follow)
- branch / cwd hints
- issue body verbatim

The dispatched agent always consumes the prompt via @file injection (pi)
or stdin (`cat` for cc/oc), so there's no quote-handling worry. Keeping
the body verbatim makes the prompt diff-friendly and lets us pin tests
against real fixtures.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from tmq.gh import IssueView, PrView

AGENTS_HEADER = """\
You are a dispatched coding agent.

You are working in the repository at: {cwd}
Branch: {branch}

Issue: {gh}#{number}
Title: {title}

{tldr}

---

"""

CHUNKING_POLICY = """\
## Chunking policy

If your plan projects a diff larger than 500 lines (insertions + deletions),
you MUST decompose the work into a sequence of ≤500-line PRs before asking
for plan approval.  Your plan MUST include:

- **Projected diff size** (estimated insertions + deletions).
- **Chunk plan** (ordered sub-PRs, each with a one-line summary and its
  dependency — "stacks on PR-1" or "independent").

### Stacked-PR safety

When chunks stack (PR-2 depends on PR-1's branch), merge them in order.
Never use `--delete-branch` when merging a stacked PR — deleting a base
branch closes every dependent PR.  Delete branches only after the full
stack lands on main.

---

"""

AC_HEADER = """\
## Acceptance criteria

Read the issue body below. Restate the AC in your own words at the top of
your first turn. If any AC is ambiguous, STOP and emit:

  <<AGENT-STATE: BLOCKED: <reason>>>

then wait for a human to clarify on the GitHub issue thread.

---

"""

AGENTS_MARKER_CONTRACT = """\
## State contract

Print exactly one of these lines at every transition:

  <<AGENT-STATE: PLAN-REVIEW>>     -- plan written, awaiting human approval
  <<AGENT-STATE: WORKING>>         -- executing (TDD / fixing / writing)
  <<AGENT-STATE: PR-REVIEW>>       -- PR open, needs code review
  <<AGENT-STATE: MERGE-READY>>     -- tests green + AC verified + review clean
  <<AGENT-STATE: BLOCKED: <why>>>  -- stuck, needs human
  <<AGENT-STATE: DONE>>            -- merged, worktree clean

Three states stop for a human: PLAN-REVIEW, PR-REVIEW (auto-fires a
review agent), MERGE-READY. Everything else is autonomous. The fleet
greps your pane for these markers; without them you are invisible.

---

"""

REVIEW_VERDICT_CONTRACT = """\
## Verdict contract (review dispatches)

Post your review as a PR COMMENT (`gh pr comment`), and end the comment
with EXACTLY one machine-parseable verdict line. The fleet poller
(`tms events scan-reviews`) parses ONLY this line — a review without it
is invisible and the PR will be re-dispatched:

  <<REVIEW-VERDICT: PASS sha=<pr-head-sha> rounds=<n> panel=<model,...>>>
  <<REVIEW-VERDICT: FAIL sha=<pr-head-sha> p0=<n> p1=<n> rounds=<n> panel=<model,...>>>

`sha=` must be the PR head at review time
(`gh pr view <num> --json headRefOid`). A GitHub PR *review*
(approve/request-changes) alone does NOT count — the verdict line must
be in a comment.

---

"""

ISSUE_HEADER_TEMPLATE = """\
## Issue body

URL: {url}

"""
PR_HEADER_TEMPLATE = """\
## PR body

URL: {url}
Base: {base_ref}
Head: {head_ref}

"""


@dataclass(frozen=True)
class PromptInput:
    cwd: str
    branch: str
    gh_slug: str
    number: int
    issue_type: str  # feature | fix | chore | review
    title: str
    tldr: str


def _header(p: PromptInput) -> str:
    return AGENTS_HEADER.format(
        cwd=p.cwd,
        branch=p.branch,
        gh=p.gh_slug,
        number=p.number,
        title=p.title,
        tldr=p.tldr,
    )


def _issue_body(issue: IssueView) -> str:
    body = (issue.body or "").strip() or "(no description)"
    return ISSUE_HEADER_TEMPLATE.format(url=issue.url) + "```markdown\n" + body + "\n```\n"


def _pr_body(pr: PrView) -> str:
    body = (pr.body or "").strip() or "(no description)"
    return (
        PR_HEADER_TEMPLATE.format(url=pr.url, base_ref=pr.base_ref, head_ref=pr.head_ref)
        + "```markdown\n"
        + body
        + "\n```\n"
    )


def build_issue_prompt(p: PromptInput, issue: IssueView) -> str:
    """Build a dispatch prompt for an issue-number input (feature/fix/chore)."""
    parts = [
        _header(p),
        AC_HEADER,
        AGENTS_MARKER_CONTRACT,
        CHUNKING_POLICY,
        _issue_body(issue),
    ]
    return "\n".join(parts)


def build_pr_prompt(p: PromptInput, pr: PrView) -> str:
    """Build a review prompt. The 'AC' for review is the PR's diff + description."""
    parts = [
        _header(p),
        AC_HEADER,
        AGENTS_MARKER_CONTRACT,
        REVIEW_VERDICT_CONTRACT,
        _pr_body(pr),
    ]
    return "\n".join(parts)


def summarize(text: str, *, lines: int = 4) -> str:
    """First non-blank lines of the body, capped.

    Used for the @file TL;DR and the dispatch event log. Truncates to
    `lines` (default 4) to keep the JSONL row compact; the full body goes
    in the prompt and the worktree cwd/branch hints cover everything an
    operator needs to reproduce the dispatch.
    """
    seen = 0
    out: list[str] = []
    for line in textwrap.dedent(text or "").splitlines():
        if not line.strip():
            if seen:
                continue
            continue
        out.append(line.rstrip())
        seen += 1
        if seen >= lines:
            break
    return "\n".join(out) if out else "(no body)"
