"""Event log writer: append rows to ``tms_review.events`` in postgres.

Direct port of ``tms/lib/tms/events.py`` from the old bash + Python tms
implementation. Differences from the original:

- No more ``~/.local/state/tmq/events.jsonl`` fallback -- the table is
  the source of truth as of tms#65 (2026-07-12). The previous JSONL is
  archived on disk and a one-time backfill would land in the same table.
- DSN resolution: explicit setting > env var > psql service=bogocat.
  The service resolution defers to ``psycopg2`` (no PGPASSWORD
  interpolation here).
- Added a ``caller`` field inside the payload dict to fix tms#67: the
  provider recorded on the row matches the model's actual provider so
  cost joins work end-to-end. Note: ``caller`` is not a top-level
  column on the table (the schema predates it); we encode it inside
  ``payload`` and the tms consumer reads it from there.

Schema lives at tms/schema/migrations/002-create-events-table.sql and is
owned by the tms repo -- tmq only writes to it. Column map:

  id              TEXT  (Python uuid.uuid4(); primary key)
  created_at      TEXT  (now isoformat)
  event_timestamp TEXT  (domain timestamp from the dispatch event)
  event_type      TEXT  (dispatch | dispatch_failed | state_transition)
  repo            TEXT  (bogocat repo short name)
  issue           INT   (issue or PR number)
  agent           TEXT  (pi | cc | oc)
  provider        TEXT  (pi provider flag value)
  model           TEXT  (pi model flag value)
  dispatch_type   TEXT  (feature | fix | chore | review)
  worktree        TEXT  (the worktree path; empty for in-place)
  session         TEXT  (aoe session name; empty on failure)
  aoe_id_prefix   TEXT  (aoe session id [..8]; join key for transitions)
  reason          TEXT  (failure hint; only on dispatch_failed)
  payload         TEXT  (canonical JSON record; json.dumps(dict))
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("tmq.events")

DEFAULT_DSN_ENV = "TMTM_DSN"
DEFAULT_SERVICE = "bogocat"
CALLER = "tmq"


class EventError(RuntimeError):
    """Appending the row failed or the DSN was missing."""


def _resolve_dsn(explicit: str = "") -> str:
    """DSN precedence: explicit arg > env var > psql service=bogocat.

    We never inline PGPASSWORD here (per the postgres-via-service rule).
    `psycopg2.connect("service=name")` walks the user's pg_service.conf,
    which is set up fleet-wide at /root/.pg_service.conf.
    """
    if explicit:
        return explicit
    env_dsn = os.environ.get(DEFAULT_DSN_ENV, "").strip()
    if env_dsn:
        return env_dsn
    # Fall through to a libpq service URI; libpq handles the lookup itself.
    return f"service={DEFAULT_SERVICE}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_event(
    *,
    event_type: str,
    repo: str,
    issue: int,
    agent: str,
    provider: str,
    model: str,
    dispatch_type: str,
    worktree: str = "",
    session: str = "",
    aoe_id_prefix: str = "",
    reason: str = "",
    payload_extra: dict[str, Any] | None = None,
    dsn: str = "",
) -> str:
    """Append one event row. Returns the new event id (string).

    ``event_type`` is one of the values documented in
    ``tms/docs/events-format.md``: ``dispatch``, ``dispatch_failed``,
    ``state_transition`` (the latter is written by the AO stale-marker
    watchdog, not by tmq).
    """
    payload: dict[str, Any] = {
        "caller": CALLER,
        "event_type": event_type,
        "event_timestamp": _now_iso(),
        "repo": repo,
        "issue": issue,
        "agent": agent,
        "provider": provider or "",
        "model": model or "",
        "dispatch_type": dispatch_type,
        "worktree": worktree,
        "session": session,
        "aoe_id_prefix": aoe_id_prefix,
        "reason": reason,
    }
    if payload_extra:
        payload.update(payload_extra)

    try:
        import psycopg2
    except ImportError as exc:
        raise EventError("psycopg2 not installed -- `pip install psycopg2-binary` in the plugin venv") from exc

    event_id = str(uuid.uuid4())
    insert_sql = (
        "INSERT INTO tms_review.events ("
        "id, created_at, event_timestamp, event_type, repo, issue, agent, "
        "provider, model, dispatch_type, worktree, session, aoe_id_prefix, "
        "reason, payload"
        ") VALUES ("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
        ")"
    )
    dsn_to_use = _resolve_dsn(dsn)
    try:
        conn = psycopg2.connect(dsn_to_use)
    except Exception as exc:
        raise EventError(f"connect to events DB failed: {exc}") from exc
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                insert_sql,
                (
                    event_id,
                    _now_iso(),
                    payload["event_timestamp"],
                    event_type,
                    repo,
                    issue,
                    agent,
                    payload["provider"],
                    payload["model"],
                    dispatch_type,
                    worktree,
                    session,
                    aoe_id_prefix,
                    reason,
                    json.dumps(payload),
                ),
            )
    except Exception as exc:
        raise EventError(f"event append failed for {event_type} {repo}#{issue}: {exc}") from exc
    finally:
        conn.close()
    return event_id


def log_dispatch(
    *,
    repo: str,
    issue: int,
    agent: str,
    provider: str,
    model: str,
    dispatch_type: str,
    cwd: str,
    session_name: str,
    aoe_id: str = "",
    status: str = "ok",
    dsn: str = "",
) -> str:
    """Convenience wrapper: the success path the bash tool emitted at the
    end of `spawn_agent`. ``status`` carries the transport (aoe/tmux)
    so the metrics rows distinguish them; aoe_id is the 8-char prefix
    the bash tool computed via ``aoe session show --json``.
    """
    return append_event(
        event_type="dispatch",
        repo=repo,
        issue=issue,
        agent=agent,
        provider=provider,
        model=model,
        dispatch_type=dispatch_type,
        worktree=cwd,
        session=session_name,
        aoe_id_prefix=aoe_id[:8] if aoe_id else "",
        payload_extra={"status": status},
        dsn=dsn,
    )


def log_dispatch_failed(
    *,
    repo: str,
    issue: int,
    agent: str,
    provider: str,
    model: str,
    dispatch_type: str,
    reason: str,
    dsn: str = "",
) -> str:
    """The failure path: tms#39 (cc root refusal), aoe add failed, aoe
    session start failed, etc. ``reason`` is the actionable hint.
    """
    return append_event(
        event_type="dispatch_failed",
        repo=repo,
        issue=issue,
        agent=agent,
        provider=provider,
        model=model,
        dispatch_type=dispatch_type,
        reason=reason,
        dsn=dsn,
    )


def recent_events(*, limit: int = 10, dsn: str = "", repo: str | None = None) -> list[dict[str, Any]]:
    """Read the most recent `limit` events from the table.

    Used by `tmq status` and the pane UI to show "what just happened".
    Returns the parsed ``payload`` JSON for each row, plus the
    ``id`` and ``created_at`` for stable ordering.
    """
    import psycopg2

    sql = "SELECT id, created_at, payload FROM tms_review.events ORDER BY created_at DESC LIMIT %s"
    args: tuple[Any, ...] = (limit,)
    if repo:
        sql += " WHERE repo = %s"
        args = (limit, repo)
    dsn_to_use = _resolve_dsn(dsn)
    try:
        conn = psycopg2.connect(dsn_to_use)
    except Exception as exc:
        raise EventError(f"connect to events DB failed: {exc}") from exc
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for event_id, ts, payload in rows:
        try:
            body = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError:
            body = {"raw": payload}
        out.append({"id": event_id, "created_at": ts, **body})
    return out


def warn(msg: str, *, stream: Any = sys.stderr) -> None:
    print(f"WARNING: {msg}", file=stream)
