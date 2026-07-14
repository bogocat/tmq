"""Tests for the worker's pane-UI push and JSON-RPC request/response.

These tests don't shell out -- they construct a Worker directly and
drive it line by line. The pane UI is the contract of interest:
``ui.state.set`` is sent on every successful dispatch/review; the
worker never panics if the host can't render the slot.
"""

from __future__ import annotations

import io
import json

from tmq.handlers import Settings
from tmq.worker import Worker


def _worker(io_in: io.StringIO, io_out: io.StringIO, **settings_overrides) -> Worker:
    """A Worker with empty bootstrap settings; tests override per case."""
    s = Settings(
        default_agent="pi",
        pi_provider="",
        pi_model="",
        registry_path="",
        events_dsn="",
        allow_root_cc=False,
        **settings_overrides,
    )
    return Worker(stdin=io_in, stdout=io_out, settings=s)


def _read_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_status_returns_worker_shape():
    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)
    w.process_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "bogocat.tmq.status", "params": {}}))
    responses = _read_lines(out)
    assert len(responses) == 1
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["runtime"] == "python"


def test_unknown_method_emits_method_not_found_error():
    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)
    w.process_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "bogocat.tmq.nope"}))
    responses = _read_lines(out)
    assert responses[0]["error"]["code"] == -32601


def test_notification_does_not_yield_response():
    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)
    w.process_line(json.dumps({"jsonrpc": "2.0", "method": "bogocat.tmq.status"}))
    assert out.getvalue() == ""


def test_dispatch_failure_yields_user_error():
    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)
    # Unknown repo
    w.process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "bogocat.tmq.dispatch",
                "params": {"args": {"repo": "no-such-repo", "issue": 245}},
            }
        )
    )
    responses = _read_lines(out)
    assert responses[0]["error"]["code"] == -32001


def test_list_repos_returns_text():
    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)
    w.process_line(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "bogocat.tmq.list_repos", "params": {}}))
    responses = _read_lines(out)
    result = responses[0]["result"]
    text = result.get("text") or " ".join(r["short"] for r in result.get("repos", []))
    assert "distillery" in text


def test_dispatch_pushes_pane_notification():
    """A successful dispatch emits a ui.state.set notification after the reply."""
    from unittest import mock

    from tmq.gh import IssueView
    from tmq.worktree import WorktreeResult

    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)

    fake_issue = IssueView(number=245, title="Rebase tooling", body="body", url="u", state="OPEN", labels=())
    fake_wt = WorktreeResult(cwd="/root/projects/distillery", branch="main")

    with (
        mock.patch("tmq.gh.fetch_issue", return_value=fake_issue),
        mock.patch("tmq.worktree.create", return_value=fake_wt),
        mock.patch(
            "tmq.spawn.spawn",
            return_value=mock.Mock(mode="aoe", session_name="feat-distillery#245", prompt_path="/tmp/x"),
        ),
        mock.patch("tmq.events.append_event", return_value="uuid-x"),
    ):
        w.process_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "bogocat.tmq.dispatch",
                    "params": {"args": {"repo": "distillery", "issue": 245}},
                }
            )
        )
    raw = out.getvalue().strip().splitlines()
    assert len(raw) >= 2, raw
    # First line is the result; second is the ui.state.set notification.
    last = json.loads(raw[-1])
    assert last["method"] == "ui.state.set"
    assert last["params"]["slot"] == "pane"
    assert last["params"]["id"] == "tmq_pane"


def test_settings_bootstrap_uses_inline_params():
    inp, out = io.StringIO(), io.StringIO()
    w = _worker(inp, out)
    # First call carries settings.
    inline = {"default_agent": "oc", "allow_root_cc": True}
    w.process_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "bogocat.tmq.status",
                "params": {"settings": inline},
            }
        )
    )
    assert w.settings.default_agent == "oc"
    assert w.settings.allow_root_cc is True
