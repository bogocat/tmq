"""Tier 1 worker -- JSON-RPC 2.0 over ndjson on stdio.

The plugin host (aoe daemon) spawns this process and pipes one
JSON-RPC request per line on stdin; we answer one reply per line on
stdout. The worker is long-lived: each dispatch is a request/response,
and the pane UI is pushed when we decide to (after every
dispatch / review call).

Concurrency model. plugin-github's worker is the reference for this
domain. For tmq the volume of host-bound RPCs is much lower (we mostly
*answer* commands; we don't poll), so we keep a single reader thread
+ a synchronous main loop. Outbound notifications (ui.state.set) are
serialized via a stdout lock so a slow flush can't drop them.

Method names. The host namespacing is ``plugin.<id>.<command>``; the
template's handler dispatches on the trailing segment so we accept both
``plugin.bogocat.tmq.dispatch`` and ``tmq.dispatch``.

Settings. The worker reads its settings either inline in the first
request's params.settings, or via env-var defaults. A real host pushes
a ``config.get`` call (per the plugin-github contract); we don't
implement the outbound RPC yet because tmq doesn't need it
(polling is the only consumer; we don't poll).

Event tracking is via the pane UI module: ``ui.state.set`` on every
dispatch / review / failure. ui.notify only fires when the operator's
session requires (overlap warning, currently).
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

from tmq.handlers import (
    HANDLERS,
    Settings,
    TmQCommandError,
    TmQExternalError,
)
from tmq.rpc import result_response
from tmq.ui_state import PaneState
from tmq.ui_state import render as render_pane

ERR_USER = -32001
ERR_EXTERNAL = -32002
ERR_INTERNAL = -32603
UI_STATE_SET = "ui.state.set"

# Defaults match tms/bin/tmq's behaviour when nothing is supplied.
DEFAULT_AGENT = "pi"
DEFAULT_PROVIDER = ""
DEFAULT_MODEL = ""
DEFAULT_REGISTRY_PATH = ""


class Worker:
    """The worker itself. Held in a thread-safe bundle.

    ``_stdout_lock`` protects against a slow stdout flush interleaving
    with an outbound notification (ui.state.set). We never read from
    stdin or write to stdout outside the methods that hold the lock
    where appropriate.
    """

    def __init__(self, *, stdin: Any, stdout: Any, settings: Settings) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.settings = settings
        self.pane = PaneState()
        self._stdout_lock = threading.Lock()
        self._outbound_id = 1
        self.repos_count = 0

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Worker -> host notification (no id, no expected reply)."""
        with self._stdout_lock:
            msg = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
            self.stdout.write(json.dumps(msg) + "\n")
            self.stdout.flush()

    def _send_result(self, msg_id: Any, result: Any) -> None:
        with self._stdout_lock:
            self.stdout.write(json.dumps(result_response(msg_id, result)) + "\n")
            self.stdout.flush()

    def _send_error(self, msg_id: Any, code: int, message: str) -> None:
        with self._stdout_lock:
            self.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": code, "message": message},
                    }
                )
                + "\n"
            )
            self.stdout.flush()

    def push_pane(self, *, repo_count: int) -> None:
        """Push current pane state to the host via ui.state.set.

        Failures are swallowed -- the pane update never fails a
        dispatch. Errors here mean the host rejected our payload and
        we'd just retry on the next event.
        """
        try:
            self._send_notification(UI_STATE_SET, render_pane(self.pane, repo_count=repo_count))
        except Exception:
            pass

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Call the matching handler or raise a typed error.

        Looks up ``HANDLERS`` on the trailing segment of the method name
        so the worker is robust to the host's exact method prefix
        (``plugin.bogocat.tmq.dispatch`` vs ``bogocat.tmq.dispatch``).
        """
        command = method.rsplit(".", 1)[-1]
        handler = HANDLERS.get(command)
        if handler is None:
            from tmq.rpc import MethodNotFoundError

            raise MethodNotFoundError(method)
        return handler(params, settings=self.settings)

    def process_line(self, line: str) -> None:
        """Read one request, dispatch, write one response.

        Side-effects: bumps the pane state on every successful
        dispatch / review. Other handlers (list_repos, status) don't
        mutate the pane.
        """
        line = (line or "").strip()
        if not line:
            return
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(request, dict):
            return
        msg_id = request.get("id")
        if msg_id is None:
            return  # notification: don't reply
        method = str(request.get("method", ""))
        params = request.get("params") or {}
        # Bootstrap: first request may carry inline settings.
        if isinstance(params, dict) and params.get("settings"):
            inline = params["settings"]
            if isinstance(inline, dict) and not self.settings_already_applied():
                self.settings = Settings.from_dict(inline)
                self._settings_applied = True  # type: ignore[attr-defined]
        try:
            result = self.dispatch(method, params)
        except LookupError as exc:
            self._send_error(msg_id, -32601, f"unknown method {exc!s}")
            return
        except TmQCommandError as exc:
            self._send_error(msg_id, ERR_USER, str(exc))
            return
        except TmQExternalError as exc:
            self._send_error(msg_id, ERR_EXTERNAL, str(exc))
            return
        except Exception as exc:
            self._send_error(msg_id, ERR_INTERNAL, str(exc))
            return
        # Side-effect: bump the pane UI on a successful dispatch/review.
        # Send the reply first so the response line is always adjacent to
        # its request line in stdout, then push the pane notification.
        self._send_result(msg_id, result)
        self._maybe_bump_pane(method, result)

    def _maybe_bump_pane(self, method: str, result: Any) -> None:
        if not (method.endswith("dispatch") or method.endswith("review")):
            return
        if not isinstance(result, dict):
            return
        try:
            self.pane.start(
                session_name=str(result.get("session_name", "")),
                repo=str(result.get("repo", "")),
                issue=int(result.get("issue", 0) or 0),
                agent=str(result.get("agent", "")),
            )
            self.pane.finish(
                session_name=str(result.get("session_name", "")),
                status=str(result.get("mode", "")),
                repo=str(result.get("repo", "")),
                issue=int(result.get("issue", 0) or 0),
                agent=str(result.get("agent", "")),
            )
            self.push_pane(repo_count=self.repos_count)
        except Exception:
            pass  # pane updates are best-effort; never fail the response.

    def settings_already_applied(self) -> bool:
        return getattr(self, "_settings_applied", False)

    def run(self) -> None:
        """Drive the loop until EOF."""
        for raw in self.stdin:
            self.process_line(raw)


def _bootstrap_settings() -> Settings:
    return Settings(
        default_agent=DEFAULT_AGENT,
        pi_provider=DEFAULT_PROVIDER,
        pi_model=DEFAULT_MODEL,
        registry_path=DEFAULT_REGISTRY_PATH,
        events_dsn="",
        allow_root_cc=False,
    )


def main(stdin: Any = None, stdout: Any = None) -> None:
    """Entry point: spawn a Worker and run it until EOF.

    ``stdin``/``stdout`` are injectable so tests can drive the worker
    without spawning a subprocess (the template's test_worker_contract.py
    uses this hook indirectly via subprocess; tests that need finer
    control call ``Worker(...).process_line`` directly).
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    Worker(stdin=stdin, stdout=stdout, settings=_bootstrap_settings()).run()


if __name__ == "__main__":
    main()
