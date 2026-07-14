"""Pane UI payload builder for `tmq_pane`.

The worker pushes ``ui.state.set`` to the host whenever a dispatch or
review completes -- this is the live tmq surface visible inside the AoE
TUI/web dashboard.

Payload shape (single source of truth -- mirrored by the Rust pane
renderer in agent-of-empires):

::

    {
        "runtime":   "python",
        "ts":        "2026-07-14T01:23:45Z",
        "repos":     20,
        "worker_id": "<uuid>",
        "in_flight": [
            {"session_name": "...", "repo": "...", "issue": 245,
             "agent": "pi", "started_at": "..."},
            ...
        ],
        "recent":    [
            {"event_type": "dispatch", "ts": "...", "repo": "...", "issue": 245,
             "agent": "pi", "status": "aoe", "session_name": "..."},
            ...
        ]
    }

The host keys UI state on ``(slot, id, session_id?)``. tmq_pane is a
global pane (no session_id), so we push once per refresh.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PaneState:
    in_flight: list[dict[str, Any]] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)

    def start(self, *, session_name: str, repo: str, issue: int, agent: str) -> None:
        self.in_flight.append(
            {
                "session_name": session_name,
                "repo": repo,
                "issue": issue,
                "agent": agent,
                "started_at": _now_iso(),
            }
        )

    def finish(self, *, session_name: str, status: str, repo: str, issue: int, agent: str) -> None:
        # Drop the matching in-flight entry, push to recent (bounded).
        self.in_flight = [e for e in self.in_flight if e["session_name"] != session_name]
        self.recent.insert(
            0,
            {
                "event_type": "dispatch",
                "ts": _now_iso(),
                "repo": repo,
                "issue": issue,
                "agent": agent,
                "status": status,
                "session_name": session_name,
            },
        )
        # Bound the list at 10 to keep the JSON small; older rows are
        # readable from events.recent_events() on demand.
        del self.recent[10:]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def worker_id() -> str:
    """Stable per-worker UUID, persisted in /tmp/tmq-pane-worker-id.

    Survives restarts so the pane can show "worker restarted at" when it
    sees a new id. First-launch generates one and writes it; subsequent
    launches read it. A missing file on first launch is fine -- generate
    one then.
    """
    path = "/tmp/tmq-pane-worker-id"
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or _mint_id(path)
    except FileNotFoundError:
        return _mint_id(path)


def _mint_id(path: str) -> str:
    new_id = str(uuid.uuid4())
    try:
        # Race-tolerant: it's a 1-line write. Two hosts starting tmq at
        # the same time will both mint an id, last writer wins.
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_id)
    except OSError:
        pass
    return new_id


def render(state: PaneState, *, repo_count: int) -> dict[str, Any]:
    """Convert a PaneState into the JSON-RPC params for ui.state.set."""
    return {
        "slot": "pane",
        "id": "tmq_pane",
        "text": _summary(state),
        "payload": {
            "runtime": "python",
            "ts": _now_iso(),
            "repos": repo_count,
            "worker_id": os.environ.get("TMQ_WORKER_ID", "") or worker_id(),
            "in_flight": state.in_flight,
            "recent": state.recent,
        },
    }


def _summary(state: PaneState) -> str:
    if not state.in_flight and not state.recent:
        return "tmq: idle"
    if state.in_flight:
        return f"tmq: {len(state.in_flight)} in flight, last {state.recent[0]['session_name'] if state.recent else '—'}"
    return f"tmq: idle, {len(state.recent)} recent"
