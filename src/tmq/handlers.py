"""Worker handlers — the four ``plugin.bogocat.tmq.<cmd>`` methods.

Each handler returns a dict that the worker wraps in a JSON-RPC
``result`` reply. Failure modes raise one of:

- ``TmQCommandError`` for user-facing input errors (unknown repo, bad
  type, missing cwd). The worker translates these to JSON-RPC ``error``
  replies with a stable code (``-32001``) so the UI can render the
  actionable hint.
- ``TmQExternalError`` for subprocess / network failures (gh, git,
  tmux, aoe). Code ``-32002``; the operator should consult stderr.

Settings (``default_agent``, ``pi_provider``, etc.) are passed in by
the worker via the ``settings`` dict. Tests construct this directly.

Key invariants (the bash predecessor had each; we keep them):

- ``--agent cc`` under root: only proceeds when ``cc_allow_root`` is
  true (tms#39).
- ``--provider`` / ``--model`` only valid with ``--agent pi`` (tms#37).
- Reviews: if an issue-number input resolves to a linked PR, return
  that PR's number; otherwise raise.
- Authentik / git remote unavailable: surface a `git remote get-url`
  failure as a registry-shape error, not a crash.

Discovery: `register_handlers()` returns the dispatch table the worker
uses. The dict key is the trailing-segment of the JSON-RPC method name
(so `plugin.bogocat.tmq.dispatch` and `bogocat.tmq.dispatch` both land
on `dispatch`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tmq import events, gh, spawn
from tmq import prompt as prompt_mod
from tmq import worktree as wt
from tmq.registry import RegistryError, RepoEntry
from tmq.registry import get as get_repo
from tmq.registry import load as load_registry

ERR_USER = -32001
ERR_EXTERNAL = -32002
ERR_INTERNAL = -32603

ALLOWED_AGENTS = {"cc", "pi", "oc"}
ALLOWED_TYPES = {"feature", "fix", "chore", "review"}


class TmQCommandError(Exception):
    """Bad input from the operator (invalid repo, type, args)."""


class TmQExternalError(Exception):
    """A subprocess or network call failed in a way the operator should see."""


@dataclass(frozen=True)
class DispatchResult:
    session_name: str
    mode: str  # "aoe" | "tmux"
    cwd: str
    branch: str
    prompt_path: str
    event_uuid: str
    repo: str
    issue: int
    agent: str
    type: str  # "feature" | "fix" | "chore" | "review"
    provider: str
    model: str

    def to_reply(self) -> dict[str, Any]:
        return {
            "session_name": self.session_name,
            "mode": self.mode,
            "cwd": self.cwd,
            "branch": self.branch,
            "prompt_path": self.prompt_path,
            "event_uuid": self.event_uuid,
            "repo": self.repo,
            "issue": self.issue,
            "agent": self.agent,
            "type": self.type,
            "provider": self.provider,
            "model": self.model,
        }


@dataclass(frozen=True)
class Settings:
    default_agent: str
    pi_provider: str
    pi_model: str
    registry_path: str
    events_dsn: str
    allow_root_cc: bool

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Settings:
        def _s(key: str, default: str = "") -> str:
            v = raw.get(key, default)
            return str(v) if v is not None else default

        def _b(key: str, default: bool) -> bool:
            v = raw.get(key)
            return bool(v) if isinstance(v, bool) else default

        agent = _s("default_agent", "pi")
        if agent not in ALLOWED_AGENTS:
            agent = "pi"
        return cls(
            default_agent=agent,
            pi_provider=_s("pi_provider"),
            pi_model=_s("pi_model"),
            registry_path=_s("registry_path"),
            events_dsn=_s("events_dsn"),
            allow_root_cc=_b("allow_root_cc", False),
        )


@dataclass(frozen=True)
class DispatchArgs:
    repo: str
    number: int
    agent: str = "pi"
    issue_type: str = "feature"
    provider: str = ""
    model: str = ""

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> DispatchArgs:
        raw = params.get("args") or {}
        repo = str(raw.get("repo", ""))
        number_raw = raw.get("issue") or raw.get("pr")
        if not repo or number_raw is None:
            raise TmQCommandError("missing required args: repo and issue (or pr)")
        try:
            number = int(number_raw)
        except (TypeError, ValueError) as exc:
            raise TmQCommandError(f"issue must be an integer, got {number_raw!r}") from exc
        return cls(
            repo=repo,
            number=number,
            agent=str(raw.get("agent", "pi")),
            issue_type=str(raw.get("type", "feature")),
            provider=str(raw.get("provider", "") or ""),
            model=str(raw.get("model", "") or ""),
        )


def _validate_agent(agent: str, *, provider: str, model: str) -> None:
    if agent not in ALLOWED_AGENTS:
        raise TmQCommandError(f"unknown agent {agent!r}; expected one of {sorted(ALLOWED_AGENTS)}")
    if (provider or model) and agent != "pi":
        raise TmQCommandError("--provider/--model only apply to --agent pi (got " + agent + ")")
    # The bash tool's `[[ "$PI_MODEL" == -* ]]` empty-value guard:
    for label, val in (("provider", provider), ("model", model)):
        if val.startswith("-"):
            raise TmQCommandError(f"--{label} expects a value (got: '{val}')")


def _validate_type(issue_type: str) -> None:
    if issue_type not in ALLOWED_TYPES:
        raise TmQCommandError(f"unknown type {issue_type!r}; expected one of {sorted(ALLOWED_TYPES)}")


def _check_reviewer_overlap(model: str) -> None:
    """tms#55 -- warn if dispatched model overlaps reviewer panel.

    The bash version of this lives in `bin/tmq:746-758` and reads the
    panel map from ``tms_review.reviewer_panel``. Until that table is
    populated (tms#57 in flight), this is a no-op + a single stderr
    line. We deliberately don't fail the dispatch on overlap; the
    warning is the contract.
    """
    if not model:
        return
    import importlib

    try:
        reviewer_panel = importlib.import_module("tmq.reviewer_panel")
    except ImportError:
        return
    try:
        overlap = reviewer_panel.overlap(model=model)
    except Exception:
        return  # Never let the warning pipeline fail a dispatch.
    if overlap:
        import sys

        print(
            f"WARNING: dispatched model {model!r} is on the reviewer panel (tms#55).",
            file=sys.stderr,
        )


def _build_dispatch_result(
    *,
    args: DispatchArgs,
    settings: Settings,
) -> tuple[DispatchResult, RepoEntry, str, str, str]:
    """Load registry, validate, fetch issue, build prompt.

    Returns the result + the entry + the cwd + branch + prompt-file path
    so the caller can wire them into the spawn step.
    """
    _validate_agent(args.agent, provider=args.provider, model=args.model)
    _validate_type(args.issue_type)
    repos = load_registry(settings.registry_path)
    entry = get_repo(repos, args.repo)
    # Fetch issue JSON; PR review path is handled separately below so we
    # keep dispatch short.
    if args.issue_type == "review":
        # Resolve issue -> PR if necessary (tms#10 review P1).
        try:
            pr = gh.fetch_pr(entry.path, entry.gh, args.number)
        except gh.GhError as exc:
            try:
                args_number_pr = gh.resolve_pr_number(entry.path, entry.gh, args.number)
            except gh.GhError:
                raise TmQExternalError(f"issue/PR lookup failed: {exc}") from exc
            pr = gh.fetch_pr(entry.path, entry.gh, args_number_pr)
        wt_res = wt.head_branch(entry, pr.head_ref)
        issue_for_prompt = None
    else:
        try:
            issue = gh.fetch_issue(entry.path, entry.gh, args.number)
        except gh.GhError as exc:
            raise TmQExternalError(f"issue fetch failed: {exc}") from exc
        wt_res = wt.create(entry, args.number, issue.title, args.issue_type)
        pr = None  # type: ignore[assignment]
        issue_for_prompt = issue
    if pr is not None:
        title = pr.title
        body_for_prompt = prompt_mod.summarize(pr.body, lines=4)
    elif issue_for_prompt is not None:
        title = issue_for_prompt.title
        body_for_prompt = prompt_mod.summarize(issue_for_prompt.body, lines=4)
    else:
        raise TmQExternalError("unreachable: neither issue nor pr populated")
    prompt_input = prompt_mod.PromptInput(
        cwd=wt_res.cwd,
        branch=wt_res.branch,
        gh_slug=entry.gh,
        number=args.number,
        issue_type=args.issue_type,
        title=title,
        tldr=body_for_prompt,
    )
    # Session name is needed before the prompt: the review prompt
    # interpolates it into the self-close teardown step (tms#138).
    session_name = spawn.session_name_for(
        repo_short=args.repo,
        number=args.number,
        issue_type=args.issue_type,
        agent=args.agent,
    )
    if pr is not None:
        body = prompt_mod.build_pr_prompt(prompt_input, pr, session_name=session_name)
    else:
        body = prompt_mod.build_issue_prompt(prompt_input, issue_for_prompt)  # type: ignore[arg-type]
    prompt_path = spawn.prompt_path_for(session_name)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(body, encoding="utf-8")
    return (
        DispatchResult(
            session_name=session_name,
            mode="pending",  # filled in by spawn step
            cwd=wt_res.cwd,
            branch=wt_res.branch,
            prompt_path=str(prompt_path),
            event_uuid="",  # filled after spawn logs
            repo=args.repo,
            issue=args.number,
            agent=args.agent,
            type=args.issue_type,
            provider=args.provider or settings.pi_provider,
            model=args.model or settings.pi_model,
        ),
        entry,
        wt_res.cwd,
        wt_res.branch,
        body,
    )


def _spawn_with_event(
    args: DispatchArgs,
    result: DispatchResult,
    settings: Settings,
) -> DispatchResult:
    """Run spawn.spawn with the right SpawnRequest, log events on both paths."""
    spawn_req = spawn.SpawnRequest(
        session_name=result.session_name,
        cwd=result.cwd,
        prompt_path=Path(result.prompt_path),
        branch=None if result.branch == "main" else result.branch,
        agent=args.agent,
        worktree=False,
        cc_allow_root=settings.allow_root_cc,
        pi_provider=result.provider or None,
        pi_model=result.model or None,
        repo_short=args.repo,
        repo_gh="",
    )
    try:
        spawned = spawn.spawn(spawn_req)
    except spawn.SpawnError as exc:
        reason = str(exc)
        try:
            event_uuid = events.log_dispatch_failed(
                repo=args.repo,
                issue=args.number,
                agent=args.agent,
                provider=result.provider,
                model=result.model,
                dispatch_type=args.issue_type,
                reason=reason,
                dsn=settings.events_dsn,
            )
        except events.EventError as log_exc:
            event_uuid = ""
            events.warn(f"event log append failed: {log_exc}")
        raise TmQExternalError(f"{reason}; event_uuid={event_uuid!r}") from exc

    # Capture the aoe id prefix when aoe is the transport, like the bash
    # tool did, so the events row joins to the running session id.
    aoe_id = ""
    if spawned.mode == "aoe":
        try:
            import json as _json
            import subprocess as _sp

            proc = _sp.run(
                ["aoe", "session", "show", result.session_name, "--json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
            if proc.returncode == 0:
                parsed = _json.loads(proc.stdout)
                if isinstance(parsed, dict) and isinstance(parsed.get("id"), str):
                    aoe_id = parsed["id"][:8]
        except Exception:
            aoe_id = ""
    event_uuid = ""
    try:
        event_uuid = events.log_dispatch(
            repo=args.repo,
            issue=args.number,
            agent=args.agent,
            provider=result.provider,
            model=result.model,
            dispatch_type=args.issue_type,
            cwd=result.cwd,
            session_name=result.session_name,
            aoe_id=aoe_id,
            status=spawned.mode,
            installed_command=spawned.installed_command,
            dsn=settings.events_dsn,
        )
    except events.EventError as exc:
        events.warn(f"event log append failed: {exc}")

    return DispatchResult(
        session_name=result.session_name,
        mode=spawned.mode,
        cwd=result.cwd,
        branch=result.branch,
        prompt_path=spawned.prompt_path,
        event_uuid=event_uuid,
        repo=result.repo,
        issue=result.issue,
        agent=result.agent,
        type=result.type,
        provider=result.provider,
        model=result.model,
    )


# ── public handlers ──────────────────────────────────────────────────────


def handle_dispatch(params: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    args = DispatchArgs.from_params(params)
    _check_reviewer_overlap(args.model or settings.pi_model)
    try:
        result, _entry, _cwd, _branch, _body = _build_dispatch_result(args=args, settings=settings)
    except RegistryError as exc:
        raise TmQCommandError(str(exc)) from exc
    result = _spawn_with_event(args=args, result=result, settings=settings)
    return result.to_reply()


def handle_review(params: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    raw = dict(params.get("args") or {})
    raw["type"] = "review"
    return handle_dispatch({"args": raw}, settings=settings)


def handle_list_repos(params: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    repos = load_registry(settings.registry_path)
    fmt = str((params.get("args") or {}).get("format", "human"))
    if fmt == "machine":
        return {"repos": [_entry_dict(r) for r in (repos[k] for k in sorted(repos))]}
    # Human format mirrors the bash tool's column output.
    lines = [
        f"{r.short:<24} {r.gh:<32} {'worktree' if r.worktree else 'in-place':<10} {r.path}"
        for r in (repos[k] for k in sorted(repos))
    ]
    return {"text": "\n".join(lines), "count": len(repos)}


def _entry_dict(r: RepoEntry) -> dict[str, Any]:
    return {"short": r.short, "path": r.path, "gh": r.gh, "worktree": r.worktree}


def handle_status(params: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    repos = load_registry(settings.registry_path)
    try:
        recent = events.recent_events(limit=10, dsn=settings.events_dsn)
    except events.EventError as exc:
        recent = []
        events.warn(f"event log read failed: {exc}")
    return {
        "runtime": "python",
        "repos": len(repos),
        "default_agent": settings.default_agent,
        "allow_root_cc": settings.allow_root_cc,
        "recent": recent,
        "ts": int(time.time()),
    }


HANDLERS: dict[str, Any] = {
    "dispatch": handle_dispatch,
    "review": handle_review,
    "list_repos": handle_list_repos,
    "status": handle_status,
}


def register_handlers() -> dict[str, Any]:
    """Public accessor for the worker; tests can also drive this directly."""
    return HANDLERS
