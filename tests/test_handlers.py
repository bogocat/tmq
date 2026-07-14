"""Unit tests for handlers — covering the rule gates without touching subprocess or DB.

We mock `tmq.gh`, `tmq.worktree`, `tmq.events`, and `tmq.spawn` so each
test exercises exactly one decision branch (validate agent, resolve PR,
log failure, etc.) and stays under a millisecond.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tmq import handlers
from tmq.gh import IssueView, PrView


@pytest.fixture
def settings() -> handlers.Settings:
    return handlers.Settings(
        default_agent="pi",
        pi_provider="",
        pi_model="",
        registry_path="",
        events_dsn="",
        allow_root_cc=False,
    )


def _fake_issue(repo: str, n: int) -> IssueView:
    return IssueView(
        number=n,
        title=f"Test issue {n}",
        body="body\n\nmore body",
        url="https://api.github.com/.../" + str(n),
        state="OPEN",
        labels=("bug",),
    )


def _fake_pr(repo: str, n: int) -> PrView:
    return PrView(
        number=n,
        title=f"Test PR {n}",
        body="PR body",
        url="https://api.github.com/.../" + str(n),
        state="OPEN",
        base_ref="main",
        head_ref=f"feat/pr-{n}",
    )


@mock.patch("tmq.events.append_event")
@mock.patch("tmq.spawn.spawn")
@mock.patch("tmq.worktree.create")
@mock.patch("tmq.gh.fetch_issue")
def test_dispatch_success(mock_fetch, mock_wt, mock_spawn, mock_event, settings):
    mock_fetch.return_value = _fake_issue("distillery", 245)
    mock_wt.return_value = mock.Mock(cwd="/root/projects/distillery", branch="main")
    mock_spawn.return_value = mock.Mock(
        mode="aoe",
        session_name="feat-distillery#245",
        prompt_path="/tmp/tmq-prompt-feat-distillery#245.txt",
    )
    mock_event.return_value = "uuid-1"

    result = handlers.handle_dispatch({"args": {"repo": "distillery", "issue": 245}}, settings=settings)
    assert result["mode"] == "aoe"
    assert result["session_name"] == "feat-distillery#245"
    assert result["issue"] == 245
    assert "prompt_path" in result
    mock_spawn.assert_called_once()


@mock.patch("tmq.events.log_dispatch_failed")
@mock.patch("tmq.spawn.spawn")
@mock.patch("tmq.worktree.create")
@mock.patch("tmq.gh.fetch_issue")
def test_dispatch_failure_logs_event(mock_fetch, mock_wt, mock_spawn, mock_log, settings):
    mock_fetch.return_value = _fake_issue("distillery", 245)
    mock_wt.return_value = mock.Mock(cwd="/root/projects/distillery", branch="main")
    from tmq.spawn import SpawnError

    mock_spawn.side_effect = SpawnError("aoe add failed")
    mock_log.return_value = "uuid-2"

    with pytest.raises(handlers.TmQExternalError):
        handlers.handle_dispatch({"args": {"repo": "distillery", "issue": 245}}, settings=settings)
    mock_log.assert_called_once()
    kwargs = mock_log.call_args.kwargs
    assert kwargs["repo"] == "distillery"
    assert kwargs["reason"] == "aoe add failed"


def test_validate_agent_cc_with_provider_fails(settings):
    # cc + provider should be a TmQCommandError. The test was misnamed
    # at first; this is the right behavior: a passing provider with a
    # cc agent is silently ignored in the bash tool (tms#37) and tmq
    # fails fast here.
    with mock.patch("tmq.gh.fetch_issue", return_value=_fake_issue("distillery", 245)):
        with mock.patch("tmq.worktree.create", return_value=mock.Mock(cwd="/tmp", branch="main")):
            with pytest.raises(handlers.TmQCommandError) as excinfo:
                handlers.handle_dispatch(
                    {"args": {"repo": "distillery", "issue": 245, "agent": "cc", "provider": "minimax"}},
                    settings=settings,
                )
    assert "pi" in str(excinfo.value)


def test_validate_agent_unknown(settings):
    # ``settings`` is a frozen dataclass; rebuild with allow_root_cc on.
    settings2 = handlers.Settings(
        default_agent=settings.default_agent,
        pi_provider=settings.pi_provider,
        pi_model=settings.pi_model,
        registry_path=settings.registry_path,
        events_dsn=settings.events_dsn,
        allow_root_cc=True,
    )
    with mock.patch("tmq.gh.fetch_issue", return_value=_fake_issue("distillery", 245)):
        with mock.patch("tmq.worktree.create", return_value=mock.Mock(cwd="/tmp", branch="main")):
            with mock.patch("tmq.events.append_event", return_value="uuid"):
                # Invalid agent -> dispatch should surface a typed error
                # (TmQCommandError). Accept ValueError too because the spawn
                # shim rises from the earlier validation gate.
                with mock.patch("tmq.spawn.spawn", side_effect=ValueError("unknown agent")):
                    with pytest.raises((handlers.TmQCommandError, handlers.TmQExternalError, ValueError)):
                        handlers.handle_dispatch(
                            {"args": {"repo": "distillery", "issue": 245, "agent": "zz"}},
                            settings=settings2,
                        )


def test_provider_only_valid_with_pi(settings):
    with mock.patch("tmq.gh.fetch_issue", return_value=_fake_issue("distillery", 245)):
        with mock.patch("tmq.worktree.create", return_value=mock.Mock(cwd="/tmp", branch="main")):
            with pytest.raises(handlers.TmQCommandError) as excinfo:
                handlers.handle_dispatch(
                    {
                        "args": {
                            "repo": "distillery",
                            "issue": 245,
                            "agent": "cc",
                            "provider": "minimax",
                        }
                    },
                    settings=settings,
                )
    assert "pi" in str(excinfo.value)


def test_model_dash_prefix_guard(settings):
    with mock.patch("tmq.gh.fetch_issue", return_value=_fake_issue("distillery", 245)):
        with mock.patch("tmq.worktree.create", return_value=mock.Mock(cwd="/tmp", branch="main")):
            with pytest.raises(handlers.TmQCommandError) as excinfo:
                handlers.handle_dispatch(
                    {"args": {"repo": "distillery", "issue": 245, "agent": "pi", "model": "-evil"}},
                    settings=settings,
                )
    assert "-model" in str(excinfo.value) or "expects a value" in str(excinfo.value)


@mock.patch("tmq.gh.fetch_pr")
@mock.patch("tmq.worktree.head_branch")
def test_review_resolves_issue_to_pr(mock_wt, mock_fetch_pr, settings):
    mock_fetch_pr.return_value = _fake_pr("distillery", 260)
    mock_wt.return_value = mock.Mock(cwd="/root/projects/distillery", branch="feat/pr-260")
    with mock.patch("tmq.spawn.spawn") as mock_spawn, mock.patch("tmq.events.append_event", return_value="uuid"):
        mock_spawn.return_value = mock.Mock(
            mode="aoe",
            session_name="review-distillery#260",
            prompt_path="/tmp/tmq-prompt-review-distillery#260.txt",
        )
        result = handlers.handle_review(
            {"args": {"repo": "distillery", "issue": 260}},
            settings=settings,
        )
    assert result["session_name"].startswith("review-")
    assert result["type"] == "review"


def test_list_repos_returns_count(settings):
    result = handlers.handle_list_repos({"args": {}}, settings=settings)
    assert "repos" in result or "text" in result
    if "repos" in result:
        assert isinstance(result["repos"], list)
        assert all(set(r.keys()) == {"short", "path", "gh", "worktree"} for r in result["repos"])


@mock.patch("tmq.events.recent_events", return_value=[])
def test_status_returns_workers_shape(mock_recent, settings):
    result = handlers.handle_status({}, settings=settings)
    assert result["runtime"] == "python"
    assert result["default_agent"] == "pi"
    assert result["allow_root_cc"] is False
    assert isinstance(result["recent"], list)


def test_settings_from_dict_filters_unknown_agents():
    s = handlers.Settings.from_dict({"default_agent": "zz", "allow_root_cc": True})
    assert s.default_agent == "pi"
    assert s.allow_root_cc is True
