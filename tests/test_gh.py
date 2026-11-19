"""Tests for the `gh` JSON wrapper.

We don't shell out for real here -- we drive `subprocess.run` via
`unittest.mock.patch` and verify the argv we send to `gh`, the JSON we
parse, and the error paths.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from tmq import gh


def _completed(stdout: str = "", stderr: str = "", rc: int = 0):
    m = mock.Mock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = rc
    return m


def test_gh_invokes_issue_view_with_correct_args():
    payload = json.dumps(
        {
            "number": 245,
            "title": "Rebase the tooling",
            "body": "long body",
            "url": "https://api.github.com/.../245",
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
    )
    with mock.patch("subprocess.run", return_value=_completed(payload, rc=0)) as p:
        view = gh.fetch_issue("/tmp", "bogocat/distillery", 245)
    cmd = p.call_args.args[0]
    assert cmd[0] == "gh"
    assert "issue" in cmd
    assert "view" in cmd
    assert "245" in cmd
    assert view.number == 245
    assert view.title == "Rebase the tooling"
    assert view.labels == ("bug",)


def test_fetch_pr_returns_pr_view():
    payload = json.dumps(
        {
            "number": 260,
            "title": "Rebase the tooling",
            "body": "PR body",
            "url": "https://api.github.com/.../260",
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": "feat/rebase",
        }
    )
    with mock.patch("subprocess.run", return_value=_completed(payload, rc=0)):
        pr = gh.fetch_pr("/tmp", "bogocat/distillery", 260)
    assert pr.number == 260
    assert pr.base_ref == "main"
    assert pr.head_ref == "feat/rebase"


def test_non_json_raises_gh_error():
    with mock.patch("subprocess.run", return_value=_completed("not json", rc=0)):
        with pytest.raises(gh.GhError) as excinfo:
            gh.fetch_issue("/tmp", "bogocat/distillery", 245)
    assert "non-JSON" in str(excinfo.value)


def test_nonzero_exit_raises_gh_error():
    with mock.patch("subprocess.run", return_value=_completed("", "Not Found", rc=1)):
        with pytest.raises(gh.GhError) as excinfo:
            gh.fetch_pr("/tmp", "bogocat/distillery", 999)
    assert "exited 1" in str(excinfo.value)
    assert "Not Found" in str(excinfo.value)


def test_gh_not_found_raises_helpful_error():

    with mock.patch("subprocess.run", side_effect=FileNotFoundError), pytest.raises(gh.GhError) as excinfo:
        gh.fetch_issue("/tmp", "bogocat/distillery", 245)
    assert "gh" in str(excinfo.value)


def test_slugify_lowercase_dashes():
    assert gh.slugify("Hello, World!") == "hello-world"
    assert gh.slugify("   ") == "x"


def test_latest_verdict_comment_returns_most_recent_verdict():
    comments = [
        "early comment, no verdict",
        "review round 1\n<<REVIEW-VERDICT: FAIL sha=aaa p0=1 p1=0 rounds=1 panel=x>>",
        "review round 2\n<<REVIEW-VERDICT: PASS sha=bbb rounds=2 panel=y>>",
    ]
    got = gh.latest_verdict_comment(comments)
    assert got is not None
    assert "PASS sha=bbb" in got


def test_latest_verdict_comment_none_when_absent():
    assert gh.latest_verdict_comment(["no verdict here", "still nothing"]) is None
    assert gh.latest_verdict_comment([]) is None
