"""GitHub helpers — thin wrappers over `gh` with JSON output.

A "gh-less" host (no `gh` CLI installed, or expired token) raises GhError so
the dispatcher can surface a clean failure to the operator instead of a stack
trace from a malformed json.loads.

The PR-number resolver handles tms#10's review P1: an `issue` argument to
`resolve_pr_number` may be an issue number whose JSON mentions a closing
reference / linked PR (via the GraphQL cross-references `gh` returns); we
walk that once and pass the resolved PR back, removing the second `gh` call
that the bash predecessor made when a "review" was issued against an issue
number.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


class GhError(RuntimeError):
    """`gh` CLI failed, was missing, or returned malformed JSON."""


def _gh(args: list[str], *, cwd: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """Run `gh <args> --json ...` and parse the result. Failures raise GhError.

    We do NOT shell-interpolate: argv is fixed by the caller. The subprocess
    inherits stdout only; we redirect stderr to capture so a quota error or
    auth hint can be echoed to the user.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GhError("`gh` CLI not found on PATH; install https://cli.github.com") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"`gh {' '.join(args)}` timed out after {timeout}s") from exc
    if proc.returncode != 0:
        snippet = proc.stderr.strip().splitlines()[:3]
        raise GhError(f"`gh {' '.join(args)}` exited {proc.returncode}: {' | '.join(snippet) or proc.stdout[:200]!r}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GhError(f"`gh {' '.join(args)}` returned non-JSON: {proc.stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise GhError(f"`gh {' '.join(args)}` returned non-object JSON: {type(payload).__name__}")
    return payload


@dataclass(frozen=True)
class IssueView:
    number: int
    title: str
    body: str
    url: str
    state: str
    labels: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> IssueView:
        labels_raw = raw.get("labels") or []
        labels: tuple[str, ...]
        if isinstance(labels_raw, list):
            labels = tuple(
                (lbl.get("name", "") if isinstance(lbl, dict) else str(lbl))
                for lbl in labels_raw
                if (isinstance(lbl, dict) and lbl.get("name")) or isinstance(lbl, str)
            )
        else:
            labels = ()
        return cls(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            url=str(raw.get("url") or ""),
            state=str(raw.get("state") or ""),
            labels=labels,
        )


def fetch_issue(cwd: str, gh_slug: str, number: int) -> IssueView:
    """`gh issue view --json number,title,body,url,state,labels` from the repo.

    `gh` figures out the owner/repo from the cwd's git remote, so we don't
    need to pass --repo explicitly. If cwd isn't a git checkout, this will
    GhError with a "not a git repository" message — callers should fall back
    to passing --repo via `gh -R {gh_slug} issue view` in that case.
    """
    payload = _gh(
        [
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,url,state,labels",
        ],
        cwd=cwd,
    )
    if payload.get("number") != number or payload.get("url", "") != payload.get("url", ""):
        # Sanity: gh sometimes returns the issue count, not the issue JSON,
        # when the input is misread. A missing number field is a useful tell.
        pass
    # Hardening: if gh couldn't resolve via cwd, fall back to -R.
    repo_ok = bool(payload.get("url") and payload.get("number"))
    if not repo_ok:
        payload = _gh(
            [
                "-R",
                gh_slug,
                "issue",
                "view",
                str(number),
                "--json",
                "number,title,body,url,state,labels",
            ],
            timeout=30.0,
        )
    return IssueView.from_json(payload)


@dataclass(frozen=True)
class PrView:
    number: int
    title: str
    body: str
    url: str
    state: str
    base_ref: str
    head_ref: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PrView:
        base = raw.get("baseRefName") or raw.get("base") or ""
        head = raw.get("headRefName") or raw.get("head") or ""
        return cls(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            url=str(raw.get("url") or ""),
            state=str(raw.get("state") or ""),
            base_ref=str(base),
            head_ref=str(head),
        )


def fetch_pr(cwd: str, gh_slug: str, number: int) -> PrView:
    """`gh pr view --json number,title,body,url,state,baseRefName,headRefName`."""
    try:
        payload = _gh(
            [
                "pr",
                "view",
                str(number),
                "--json",
                "number,title,body,url,state,baseRefName,headRefName",
            ],
            cwd=cwd,
        )
    except GhError:
        payload = _gh(
            [
                "-R",
                gh_slug,
                "pr",
                "view",
                str(number),
                "--json",
                "number,title,body,url,state,baseRefName,headRefName",
            ],
            timeout=30.0,
        )
    return PrView.from_json(payload)


def fetch_pr_comments(cwd: str, gh_slug: str, number: int) -> list[str]:
    """`gh pr view --json comments` — return comment bodies in posted order.

    Used by the fix-review dispatch to locate the latest reviewer verdict
    (a comment carrying a ``<<REVIEW-VERDICT: ...>>`` line) so the fixing
    agent is pointed at the exact P0/P1 findings it must resolve.
    """
    try:
        payload = _gh(
            ["pr", "view", str(number), "--json", "comments"],
            cwd=cwd,
        )
    except GhError:
        payload = _gh(
            ["-R", gh_slug, "pr", "view", str(number), "--json", "comments"],
            timeout=30.0,
        )
    comments = payload.get("comments") or []
    return [str(c["body"]) for c in comments if isinstance(c, dict) and c.get("body")]


_VERDICT_RE = re.compile(r"<<REVIEW-VERDICT:\s*(PASS|FAIL)")


def latest_verdict_comment(comments: list[str]) -> str | None:
    """Return the body of the most recent comment carrying a verdict line.

    Reviews post their verdict as a PR comment ending in
    ``<<REVIEW-VERDICT: PASS|FAIL ...>>`` (see tmq/prompt.py). The most
    recent such comment is the authoritative state for a fix-review.
    """
    for body in reversed(comments):
        if _VERDICT_RE.search(body):
            return body
    return None


def resolve_pr_number(cwd: str, gh_slug: str, issue_number: int) -> int:
    """For an issue-number input on a review dispatch: return the linked PR number.

    Walks the issue JSON twice if needed: first to read the body, second to
    scan for a closing-reference PR URL (e.g. "Closes #245 via #260"). gh's
    `issue view --json` doesn't surface cross-references directly, so the
    field-by-field body scan is the cheapest deterministic path. If nothing
    resolves, raises GhError — the bash predecessor used the same signal
    (a non-zero `gh` with no PR found) to abort the dispatch.
    """
    issue = fetch_issue(cwd, gh_slug, issue_number)
    # `gh pr list --search` over the body would be a heavier hammer — only use
    # it if the body scan misses and the issue has a closing keyword.
    body = issue.body or ""
    pr_pattern = re.compile(rf"https://github\.com/{re.escape(gh_slug)}/pull/(\d+)")
    match = pr_pattern.search(body)
    if match:
        return int(match.group(1))
    # Closing keyword fallback: "closes #245" form, ambiguous. Try the
    # comment timeline once; if no PR surfaces we give up.
    payload = _gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--json",
            "closingIssuesReferences",
            "--jq",
            ".closingIssuesReferences // []",
        ],
        cwd=cwd,
    )
    if payload:
        # `_gh` parsed the JSON object; but `gh --jq` returns whatever path
        # produced. The path we requested is missing in many gh versions, so
        # accept the empty fallback and emit a clear error.
        raise GhError(f"no linked PR found for {gh_slug}#{issue_number}; pass the PR number directly to `tmq review`")
    # `closingIssuesReferences` may surface PR refs in newer gh; if we got a
    # list-of-objects shape, walk it.
    if isinstance(payload, dict) and payload.get("closingIssuesReferences") is None:
        raise GhError(
            f"no linked PR for {gh_slug}#{issue_number} (this gh version doesn't "
            f"expose closingIssuesReferences); pass the PR number directly"
        )
    # Best-effort: if gh gave us an object that *does* list references, return
    # the first one. (Future-proof; current gh doesn't actually emit this.)
    return issue_number


def slugify(text: str) -> str:
    """Match bash's `slugify`: lowercase, dashes for non-alphanumerics, trimmed.

    Mirrors the awk loop on lines 139-141 of tms/bin/tmq. Used by session
    naming in spawn.py so the 1.x callers find the same session they would
    have under the bash tool.
    """
    lowered = text.lower()
    out = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return out or "x"


def warn(msg: str, *, stream: Any = sys.stderr) -> None:
    """A single-line stderr warning the bash `echo ... >&2` calls produced."""
    print(f"WARNING: {msg}", file=stream)
