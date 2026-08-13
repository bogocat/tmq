"""`tmq` CLI: argparse wrapper around the same functions the worker uses.

This is the operator-facing entry point (`tmq <repo> <n>`, `tmq review`,
etc.). It is intentionally thin: the worker and the CLI both call the
same handler functions in `tmq.handlers`, so there's exactly one
implementation per dispatch path. The CLI adds stdout formatting,
exit codes, and an `argparse` parser; the worker adds JSON-RPC
envelopes.

Settings are sourced from environment variables when no setting
override is given (the worker gets them via config.get):

- TMQ_DEFAULT_AGENT
- TMQ_PI_PROVIDER
- TMQ_PI_MODEL
- TMQ_REGISTRY_PATH
- TMQ_EVENTS_DSN
- TMQ_ALLOW_ROOT_CC  (also accepts the bash tool's TMQ_ALLOW_ROOT_CC)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from tmq.handlers import (
    HANDLERS,
    Settings,
    TmQCommandError,
    TmQExternalError,
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def settings_from_env() -> Settings:
    return Settings.from_dict(
        {
            "default_agent": _env("TMQ_DEFAULT_AGENT", "pi"),
            "pi_provider": _env("TMQ_PI_PROVIDER"),
            "pi_model": _env("TMQ_PI_MODEL"),
            "registry_path": _env("TMQ_REGISTRY_PATH"),
            "events_dsn": _env("TMQ_EVENTS_DSN"),
            "allow_root_cc": _env("TMQ_ALLOW_ROOT_CC") in {"1", "true", "yes", "on"},
        }
    )


def build_parser() -> argparse.ArgumentParser:
    """Match the bash tool's surface; positional args always come first.

    The first positional can be:

    - a repo short name (``tmq distillery 245``)
    - the literal ``list`` / ``status`` / ``review`` / ``pr``

    Argparse doesn't natively support this kind of late dispatch, so we
    collect everything into positional placeholders and dispatch on the
    contents after parsing. Mirror the bash tool one-for-one:

    - ``tmq <repo> <number>``               -- issue dispatch
    - ``tmq review <repo> <pr>``            -- review dispatch
    - ``tmq pr <repo> <pr>``                -- alias for review
    - ``tmq list`` / ``tmq status``         -- queries
    """
    p = argparse.ArgumentParser(
        prog="tmq",
        description="tmq -- spawn a coding agent on a GitHub issue",
    )
    p.add_argument(
        "first",
        nargs="?",
        help="Repo short name OR a subcommand (list/status/review/pr).",
    )
    p.add_argument(
        "second",
        nargs="?",
        help="Issue/PR number when the first arg is a repo; repo short for review/pr.",
    )
    p.add_argument(
        "third",
        nargs="?",
        help="PR number when the first arg is review/pr.",
    )
    p.add_argument("--agent", choices=["cc", "pi", "oc", "dsh"], help="Coding agent to spawn")
    p.add_argument("--type", choices=["feature", "fix", "chore", "review"], help="Dispatch type")
    p.add_argument("--provider", help="Override the pi provider flag (pi only)")
    p.add_argument("--model", help="Override the pi model flag (pi only)")
    return p


def _emit(handler_key: str, params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    handler = HANDLERS[handler_key]
    try:
        return handler(params, settings=settings)
    except TmQCommandError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    except TmQExternalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(3)


def main(argv: list[str] | None = None) -> int:
    """Single entry point for the `tmq` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = settings_from_env()

    cmd = (args.first or "").lower()
    second = args.second
    third = args.third

    # Query subcommands.
    if cmd == "list":
        result = _emit("list_repos", {"args": {"format": "human"}}, settings)
        print(result.get("text", ""))
        return 0
    if cmd == "status":
        result = _emit("status", {}, settings)
        runtime = result.get("runtime", "?")
        repos = result.get("repos", 0)
        print(
            f"runtime: {runtime}, repos: {repos}, default_agent: "
            f"{result.get('default_agent')}, allow_root_cc: {result.get('allow_root_cc')}"
        )
        recent = result.get("recent", [])
        if recent:
            print("\nlast 10 events:")
            for ev in recent:
                ts = ev.get("created_at", "?")
                et = ev.get("event_type", "?")
                repo = ev.get("repo", "?")
                num = ev.get("issue", "?")
                agent = ev.get("agent", "?")
                status = (ev.get("extra") or {}).get("status", "")
                print(f"  {ts} {et:<15} {repo}#{num} {agent} {status}")
        return 0

    # Review shortcut: `tmq review <repo> <pr>` / `tmq pr <repo> <pr>`.
    if cmd in {"review", "pr"}:
        if not (second and third):
            parser.error(f"`tmq {cmd}` requires <repo> and <pr>")
            return 2
        try:
            number = int(third)
        except ValueError:
            parser.error(f"PR must be an integer (got {third!r})")
            return 2
        return _dispatch(
            repo=second,
            number=number,
            agent=args.agent,
            type_override="review",
            provider=args.provider,
            model=args.model,
            settings=settings,
        )

    # Default: `tmq <repo> <number>`.
    if cmd and second is not None:
        try:
            number = int(second)
        except ValueError:
            parser.error(f"issue must be an integer (got {second!r})")
            return 2
        return _dispatch(
            repo=cmd,
            number=number,
            agent=args.agent,
            type_override=args.type,
            provider=args.provider,
            model=args.model,
            settings=settings,
        )

    parser.print_help()
    return 1


def _dispatch(
    *,
    repo: str,
    number: int,
    agent: str | None,
    type_override: str | None,
    provider: str | None,
    model: str | None,
    settings: Settings,
) -> int:
    """Outer dispatch: assemble args -> handler params -> emit."""
    params: dict[str, Any] = {
        "args": {
            "repo": repo,
            "issue": number,
            "agent": agent or settings.default_agent,
            "type": type_override or "feature",
            "provider": provider or "",
            "model": model or "",
        }
    }
    result = _emit("dispatch", params, settings)
    mode = result.get("mode")
    session_name = result.get("session_name", "")
    if mode == "aoe":
        monitor = "aoe status"
    else:
        monitor = f"tmux a -t {session_name}"
    print(f"=== tmq: dispatched ({mode}) ===")
    print(f"  session:  {session_name}")
    print(f"  cwd:      {result.get('cwd')}")
    print(f"  branch:   {result.get('branch')}")
    print(f"  prompt:   {result.get('prompt_path')}")
    print(f"  monitor:  {monitor}")
    print(f"  event:    {result.get('event_uuid')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
