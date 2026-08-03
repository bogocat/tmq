"""Tests for prompt construction — chunking policy, AC headers, marker contract."""

from __future__ import annotations

from tmq import prompt
from tmq.gh import IssueView


def _fake_issue(body: str = "Test body") -> IssueView:
    return IssueView(
        number=9,
        title="feat: plan-gate PR chunking",
        body=body,
        url="https://github.com/bogocat/tmq/issues/9",
        state="OPEN",
        labels=("enhancement",),
    )


def _prompt_input() -> prompt.PromptInput:
    return prompt.PromptInput(
        cwd="/root/wt-tmq-9",
        branch="feat/issue-9-plan-gate-pr-chunking",
        gh_slug="bogocat/tmq",
        number=9,
        issue_type="feature",
        title="feat: plan-gate PR chunking",
        tldr="Enforce PR chunking at the plan gate",
    )


def test_prompt_includes_chunking_policy():
    """The dispatch prompt must include the chunking policy directive."""
    pi = _prompt_input()
    issue = _fake_issue()
    output = prompt.build_issue_prompt(pi, issue)
    assert prompt.CHUNKING_POLICY in output, "prompt must include CHUNKING_POLICY constant"


def test_prompt_chunking_mentions_threshold():
    """The chunking clause must reference the 500-line threshold."""
    pi = _prompt_input()
    issue = _fake_issue()
    output = prompt.build_issue_prompt(pi, issue)
    assert "500" in output, "prompt must mention the 500-line threshold"


def test_prompt_chunking_mentions_stacked_pr_gotcha():
    """The chunking clause must warn about stacked-PR --delete-branch hazard."""
    pi = _prompt_input()
    issue = _fake_issue()
    output = prompt.build_issue_prompt(pi, issue)
    assert "delete-branch" in output.lower(), "prompt must mention the --delete-branch gotcha"


def test_prompt_structure_includes_marker_contract():
    """Existing invariant: the marker contract must still be present."""
    pi = _prompt_input()
    issue = _fake_issue()
    output = prompt.build_issue_prompt(pi, issue)
    assert prompt.AGENTS_MARKER_CONTRACT in output, "prompt must include AGENTS_MARKER_CONTRACT constant"


def test_prompt_structure_includes_ac_header():
    """Existing invariant: AC header must still be present."""
    pi = _prompt_input()
    issue = _fake_issue()
    output = prompt.build_issue_prompt(pi, issue)
    assert prompt.AC_HEADER in output, "prompt must include AC_HEADER constant"


def test_prompt_section_ordering():
    """Sections must appear in order: AC header < marker contract < chunking policy < issue body."""
    pi = _prompt_input()
    issue = _fake_issue()
    output = prompt.build_issue_prompt(pi, issue)
    ac_pos = output.index("Acceptance criteria")
    contract_pos = output.index("State contract")
    chunking_pos = output.index("Chunking policy")
    body_pos = output.index("Issue body")
    assert ac_pos < contract_pos < chunking_pos < body_pos, (
        f"section order wrong: AC={ac_pos} contract={contract_pos} chunking={chunking_pos} body={body_pos}"
    )


def test_pr_prompt_excludes_chunking_policy():
    """Chunking policy must NOT leak into PR review prompts."""
    from tmq.gh import PrView

    pi = _prompt_input()
    pr = PrView(
        number=10,
        title="Test PR",
        body="PR body",
        url="https://github.com/bogocat/tmq/pull/10",
        state="OPEN",
        base_ref="main",
        head_ref="feat/test",
    )
    output = prompt.build_pr_prompt(pi, pr, session_name="review-tmq#10")
    assert prompt.CHUNKING_POLICY not in output, "CHUNKING_POLICY must not leak into PR review prompts"


def test_pr_prompt_teaches_review_verdict_contract():
    """Review prompts must instruct the REVIEW-VERDICT comment line.

    The fleet poller (tms events scan-reviews) parses ONLY
    <<REVIEW-VERDICT: ...>> comment lines; review prompts taught only
    <<AGENT-STATE: ...>>, so reviewers posted AGENT-STATE / bare GitHub
    PR reviews and the poller waited forever — ~20 PRs sat with idle
    verdict-less reviewers for days (2026-07-30 → 2026-08-03).
    """
    from tmq.gh import PrView

    pr = PrView(
        number=10,
        title="Test PR",
        body="PR body",
        url="https://github.com/bogocat/tmq/pull/10",
        state="OPEN",
        base_ref="main",
        head_ref="feat/test",
    )
    output = prompt.build_pr_prompt(_prompt_input(), pr, session_name="review-tmq#10")
    assert "<<REVIEW-VERDICT: PASS sha=" in output
    assert "<<REVIEW-VERDICT: FAIL sha=" in output
    assert "gh pr comment" in output


def test_issue_prompt_excludes_verdict_contract():
    """The verdict contract is review-only; issue prompts must not see it."""
    output = prompt.build_issue_prompt(_prompt_input(), _fake_issue())
    assert "REVIEW-VERDICT" not in output


# ── tms#138: reviewer lifecycle — one marker per role + self-close ──


def _fake_pr():
    from tmq.gh import PrView

    return PrView(
        number=10,
        title="Test PR",
        body="PR body",
        url="https://github.com/bogocat/tmq/pull/10",
        state="OPEN",
        base_ref="main",
        head_ref="feat/test",
    )


def test_pr_prompt_excludes_agent_state_contract():
    """Review prompts must NOT carry the author state contract — reviewers
    were emitting MERGE-READY (an author-only signal) and idling forever."""
    output = prompt.build_pr_prompt(_prompt_input(), _fake_pr(), session_name="review-tmq#10")
    assert prompt.AGENTS_MARKER_CONTRACT not in output, (
        "AGENTS_MARKER_CONTRACT must not appear in review prompts (author-only)"
    )
    assert "<<AGENT-STATE: MERGE-READY>>" not in output, "review prompt must not teach the MERGE-READY marker"


def test_pr_prompt_forbids_agent_state_markers():
    """Review prompts must explicitly forbid AGENT-STATE and name
    MERGE-READY as never a reviewer's call."""
    output = prompt.build_pr_prompt(_prompt_input(), _fake_pr(), session_name="review-tmq#10")
    assert "Do NOT print <<AGENT-STATE" in output, "review prompt must forbid AGENT-STATE markers"
    assert "author-only" in output
    assert "MERGE-READY" in output


def test_pr_prompt_self_closes_after_verdict():
    """After the verdict comment is posted the reviewer must tear down its
    own session — aoe rm --purge with a tmux kill-session fallback,
    interpolating the real session name."""
    output = prompt.build_pr_prompt(_prompt_input(), _fake_pr(), session_name="review-tmq#10")
    assert 'aoe rm "review-tmq#10" --purge' in output, "review prompt lost the aoe rm --purge self-close step"
    assert 'tmux kill-session -t "review-tmq#10"' in output, "review prompt lost the tmux kill-session fallback"
    assert "--delete-worktree" not in output, "self-close must never delete a (possibly shared) worktree"


def test_issue_prompt_keeps_author_contract_and_no_lifecycle():
    """Author prompts keep the AGENT-STATE contract and never see the
    reviewer lifecycle section."""
    output = prompt.build_issue_prompt(_prompt_input(), _fake_issue())
    assert prompt.AGENTS_MARKER_CONTRACT in output
    assert "Reviewer lifecycle" not in output
    assert "aoe rm" not in output


def test_marker_contract_marked_author_only():
    """The state-contract text itself must say it is author-only, so a
    reviewer that sees it quoted elsewhere still knows not to use it."""
    assert "author" in prompt.AGENTS_MARKER_CONTRACT.lower(), (
        "AGENTS_MARKER_CONTRACT must state it applies to authors only"
    )
