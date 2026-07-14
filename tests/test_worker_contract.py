"""JSON-RPC worker contract tests -- exercise the protocol surface without spinning up the plugin host.

The plugin-template shipped these and they're the cheapest sanity check
that the worker speaks ndjson JSON-RPC 2.0 correctly. Updated for the
real tmq handlers; the cookiecutter's `{ok, runtime, message}` placeholder
shape was replaced by `handle_status`'s real output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")


def run_worker(requests):
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    env = {**os.environ, "PYTHONPATH": SRC}
    proc = subprocess.run(
        [sys.executable, "-m", "tmq.worker"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=env,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_status_returns_worker_shape():
    responses = run_worker([{"jsonrpc": "2.0", "id": 1, "method": "bogocat.tmq.status", "params": {}}])
    assert len(responses) == 1
    assert responses[0]["id"] == 1
    result = responses[0]["result"]
    assert result["runtime"] == "python"
    assert isinstance(result["repos"], int)
    assert result["default_agent"] in {"pi", "cc", "oc"}


def test_list_repos_returns_keys():
    responses = run_worker([{"jsonrpc": "2.0", "id": 1, "method": "bogocat.tmq.list_repos", "params": {}}])
    result = responses[0]["result"]
    # Machine output is the default (`format` arg not passed); the handler
    # gives the human-readable "text" form when format != "machine". The
    # plugin-host TUI calls the worker without format so we just assert a
    # text or repos key exists and the count is positive.
    text = result.get("text") or ""
    if not text and "repos" in result:
        text = "\n".join(r["short"] for r in result["repos"])
    assert "distillery" in text
    assert "home-portal" in text


def test_unknown_method_errors():
    responses = run_worker([{"jsonrpc": "2.0", "id": 2, "method": "bogocat.tmq.nope"}])
    assert responses[0]["error"]["code"] == -32601
    assert "bogocat.tmq.nope" in responses[0]["error"]["message"]


def test_notification_has_no_response():
    responses = run_worker([{"jsonrpc": "2.0", "method": "bogocat.tmq.status"}])
    assert responses == []


def test_invalid_command_input_returns_user_error():
    # Unknown repo: a -32001 (TmQCommandError) reply, not -32601 nor -32603.
    responses = run_worker(
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "bogocat.tmq.dispatch",
                "params": {"args": {"repo": "no-such-repo", "issue": 245}},
            }
        ]
    )
    assert responses[0]["error"]["code"] == -32001
