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
    output = prompt.build_pr_prompt(pi, pr)
    assert prompt.CHUNKING_POLICY not in output, "CHUNKING_POLICY must not leak into PR review prompts"
