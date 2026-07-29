"""Agent spawn: drive `aoe add` + `aoe session start`, fall back to raw tmux.

Order of attempts (mirrors tms/bin/tmq's `spawn_agent`):

1. If `command -v aoe` succeeds AND we're not under a half-broken state,
   `aoe add <cwd> -t <name> --tool <aoe_tool> [-w <branch>] --trust-hooks
   --cmd-override <cmd>` then `aoe session start <name>`.
2. If `aoe add` fails (duplicate title, missing worktree, etc.), surface
   the error verbatim and clean up the half-registered session.
3. If `aoe` is missing entirely, fall back to raw `tmux new-session -d`
   with the same cmd_override.

`--dangerously-skip-permissions` for `claude` is gated on the cc-root
guard (tms#39); under root/sudo without `allow_root_cc` we fail fast.

We log dispatch / dispatch_failed rows in events.py via the `caller`
passed by the handler. Keeping the spawn module pure-Python lets the
handlers test it with mocked subprocess without spinning up tmux.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CC_ROOT_ENV = "TMQ_ALLOW_ROOT_CC"
PROMPT_DIR = "/tmp"

# tms#117: `aoe add` for an existing (title, path) prints "Session already
# exists with same title and path" and EXITS 0 — a silent no-op that keeps
# the old stored command. The exit code is not a signal; this marker is.
DUPLICATE_MARKER = "already exists"


class SpawnError(RuntimeError):
    """Spawn failed; the handler decides whether to retry or surface."""


@dataclass(frozen=True)
class SpawnRequest:
    session_name: str
    cwd: str
    prompt_path: Path
    branch: str | None
    agent: str
    worktree: bool
    cc_allow_root: bool
    pi_provider: str | None
    pi_model: str | None
    repo_short: str
    repo_gh: str


@dataclass(frozen=True)
class SpawnResult:
    mode: str  # "aoe" | "tmux" | "refused"
    session_name: str
    prompt_path: str
    monitor_hint: str  # "aoe status" vs "tmux a -t <name>"
    failure_reason: str | None = None
    # tms#117: the command verifiably installed for the session — read back
    # from `aoe session show --json` (aoe mode) or constructed verbatim
    # (tmux mode). Empty = could not be verified (aoe schema drift).
    installed_command: str = ""

    @property
    def ok(self) -> bool:
        return self.mode in {"aoe", "tmux"} and self.failure_reason is None


def _modern_node_prefix() -> str:
    """Inline PATH prefix pinning the newest nvm node >= 22 for the pane.

    Panes spawned from cron or the long-lived aoe daemon can inherit a
    PATH where /usr/bin/node (distro v20) precedes nvm — and pi is a
    node script (``#!/usr/bin/env node``) whose bundled undici crashes
    on Node 20 at startup (``webidl.util.markAsUncloneable``), killing
    dispatched reviewers seconds after spawn. Mirrors
    ``tms/bin/tmq:tmq_node22_prefix``. Best-effort: no nvm install →
    ``""`` → inherited PATH (previous behavior).
    """
    nvm_dir = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    versions = nvm_dir / "versions" / "node"
    try:
        entries = list(versions.iterdir())
    except OSError:
        return ""
    best: tuple[int, int, int] | None = None
    best_path: Path | None = None
    for d in entries:
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", d.name)
        if not m:
            continue
        ver = (int(m[1]), int(m[2]), int(m[3]))
        if ver < (22, 0, 0) or not (d / "bin" / "node").is_file():
            continue
        if best is None or ver > best:
            best, best_path = ver, d
    if best_path is None:
        return ""
    return f"PATH='{best_path}/bin':\"$PATH\" "


def _is_root() -> bool:
    return os.geteuid() == 0


def _aoe_tool_name(agent: str) -> str:
    # aoe recognises these tool names (matches tms/bin/tmq:386-390).
    return {"cc": "claude", "pi": "pi", "oc": "opencode"}[agent]


def _build_cmd_override(req: SpawnRequest) -> str:
    """Construct the shell command the agent pane executes.

    The bash equivalent at tms/bin/tmq:417-441. Two non-obvious bits:

    - The prompt is piped via `cat ... |` for cc/oc so `--plan`/`--word`
      in the issue body can never become a stray argv word (tms#42).
    - pi reads the prompt via `@file` injection, so we never have to worry
      about same argv split.
    """
    p = str(req.prompt_path)
    if req.agent == "cc":
        prefix = ""
        if _is_root():
            if req.cc_allow_root:
                prefix = "IS_SANDBOX=1 "
            else:
                raise SpawnError(
                    "cc dispatch refused under root: pass allow_root_cc=true or run with TMQ_ALLOW_ROOT_CC=1 to opt in"
                )
        return (
            f"cat '{p}' | {prefix}claude --dangerously-skip-permissions -p; echo; echo '--- CLAUDE DONE ---'; exec bash"
        )
    if req.agent == "pi":
        extra = ""
        if req.pi_provider:
            extra += f" --provider {req.pi_provider}"
        if req.pi_model:
            extra += f" --model {req.pi_model}"
        return f"{_modern_node_prefix()}PI_DISPATCH_AUTOAPPROVE=1 pi{extra} --approve @{p}; echo; echo '--- PI DONE ---'; exec bash"
    if req.agent == "oc":
        return f"cat '{p}' | opencode 2>&1; echo; echo '--- OPENCODE DONE ---'; exec bash"
    raise SpawnError(f"unknown agent: {req.agent!r}")


def _run(argv: list[str], *, check: bool = True, timeout: float = 30.0) -> str:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SpawnError(f"{argv[0]!r} not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpawnError(f"`{' '.join(argv)}` timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        snippet = proc.stderr.strip().splitlines()[:3] or [proc.stdout[:200]]
        raise SpawnError(f"`{' '.join(argv)}` exited {proc.returncode}: {' | '.join(snippet)}")
    return proc.stdout


def _spawn_via_aoe(req: SpawnRequest, cmd_override: str) -> SpawnResult:
    """Use the Agent of Empires daemon when it's installed.

    Failure modes:
    - `aoe add` exit != 0: capture the output, raise SpawnError.
    - `aoe session start` exit != 0: the worktree is already registered
      by `aoe add`; we need to `aoe remove <name>` to leave the
      dashboard clean before re-raising.
    """
    if shutil.which("aoe") is None:
        return _spawn_via_tmux(req, cmd_override)
    aoe_tool = _aoe_tool_name(req.agent)
    wt_flags: list[str] = []
    if req.branch:
        wt_flags = ["-w", req.branch]

    # Best-effort purge of any stale session record.  `aoe rm` without
    # `--purge` leaves the session record with a stale `command:` field,
    # so `aoe add` for the same title re-registers silently with the OLD
    # command.  `--purge` drops the full record; if the session does not
    # exist `aoe rm` exits non-zero and we carry on (the `aoe add` below
    # will surface any real error).
    try:
        _run(["aoe", "rm", "--purge", req.session_name], check=False)
    except SpawnError:
        pass

    add_argv = [
        "aoe",
        "add",
        req.cwd,
        "-t",
        req.session_name,
        "--tool",
        aoe_tool,
        *wt_flags,
        "--trust-hooks",
        "--cmd-override",
        cmd_override,
    ]
    try:
        add_out = _run(add_argv)
    except SpawnError as exc:
        raise SpawnError(f"aoe add failed: {exc}") from exc
    # tms#117: the rm --purge above is best-effort. If the stale record
    # survived, `aoe add` no-ops with exit 0 and the duplicate marker —
    # purge again and retry once; a second duplicate fails loud (never
    # start a session whose stored command we didn't install).
    if DUPLICATE_MARKER in add_out.lower():
        try:
            _run(["aoe", "rm", "--purge", req.session_name], check=False)
        except SpawnError:
            pass
        try:
            add_out = _run(add_argv)
        except SpawnError as exc:
            raise SpawnError(f"aoe add failed on retry: {exc}") from exc
        if DUPLICATE_MARKER in add_out.lower():
            raise SpawnError(
                f"aoe still reports an existing session for {req.session_name!r} "
                f"after `aoe rm --purge`; the stale registration (and its old "
                f"stored command) survived — refusing to start it (tms#117)"
            )
    # tms#117: post-add verification — never trust that the registration we
    # just made is the one that will run. Read back the stored command and
    # require an exact match. None (unreadable) degrades to unverified.
    installed = _stored_session_command(req.session_name)
    if installed is not None and installed != cmd_override:
        raise SpawnError(
            f"aoe stored a DIFFERENT command than requested for "
            f"{req.session_name!r} (stored={installed!r}); purge with "
            f"`aoe rm {req.session_name} --purge` and re-dispatch (tms#117)"
        )
    try:
        _run(["aoe", "session", "start", req.session_name])
    except SpawnError as exc:
        # Clean up the half-registered session so it doesn't dangle in
        # `aoe list`. Best-effort; the session-start failure is the more
        # actionable error.
        try:
            _run(["aoe", "remove", req.session_name], check=False)
        except SpawnError:
            pass
        raise SpawnError(f"aoe session start failed: {exc}") from exc
    return SpawnResult(
        mode="aoe",
        session_name=req.session_name,
        prompt_path=str(req.prompt_path),
        monitor_hint="aoe status",
        installed_command=installed or "",
    )


def _stored_session_command(session_name: str) -> str | None:
    """Read back the command aoe stored for ``session_name``.

    Returns the stored command string, or None when it cannot be read
    (session missing, aoe schema drift, unparseable JSON). None means
    "unverifiable", not "mismatch" — the caller decides how loud to be.
    """
    try:
        out = _run(["aoe", "session", "show", session_name, "--json"])
    except SpawnError:
        return None
    try:
        parsed = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    cmd = parsed.get("command")
    return cmd if isinstance(cmd, str) else None


def _spawn_via_tmux(req: SpawnRequest, cmd_override: str) -> SpawnResult:
    """Fallback: raw tmux new-session. No dashboard visibility, no aoe id.

    Identical to tms/bin/tmq:480-505. The acp-add-then-tmux fallback in
    the bash tool works the same; we only collapse the branch-not-set
    case into a single argv.
    """
    argv = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        req.session_name,
        "-c",
        req.cwd,
        cmd_override,
    ]
    try:
        _run(argv)
    except SpawnError as exc:
        raise SpawnError(f"tmux spawn failed: {exc}") from exc
    return SpawnResult(
        mode="tmux",
        session_name=req.session_name,
        prompt_path=str(req.prompt_path),
        monitor_hint=f"tmux a -t {req.session_name}",
        # tmux installs cmd_override verbatim — no read-back surface exists.
        installed_command=cmd_override,
    )


def spawn(req: SpawnRequest) -> SpawnResult:
    """Top-level: write prompt file, pick a transport, return SpawnResult.

    The prompt file write is idempotent and overwrites on each call so a
    re-dispatch always uses the latest prompt. The bash tool wrote to
    `/tmp/tmq-prompt-<name>.txt`; we keep the same path for compat with
    any external prompt auditors.
    """
    req.prompt_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = _build_cmd_override(req)
    try:
        aoe_result = _spawn_via_aoe(req, cmd)
    except SpawnError:
        raise
    # If aoe is missing entirely, fall back to raw tmux.
    try:
        if shutil.which("aoe") is None:
            return _spawn_via_tmux(req, cmd)
    except SpawnError as exc:
        return SpawnResult(
            mode="refused",
            session_name=req.session_name,
            prompt_path=str(req.prompt_path),
            monitor_hint="",
            failure_reason=str(exc),
        )
    if aoe_result is not None:
        return aoe_result
    # aoe present but `_spawn_via_aoe` returned None only when the helper
    # itself bailed (shouldn't happen given the guard above, but cheap).
    return _spawn_via_tmux(req, cmd)


def session_name_for(*, repo_short: str, number: int, issue_type: str, agent: str) -> str:
    """Match the bash naming: ``<type>-<short>#<num>[-agent]``.

    ``-agent`` suffix only for cc/oc -- pi is the default, no suffix
    (matches tms/bin/tmq:849-855).
    """
    base = {
        "feature": f"feat-{repo_short}#{number}",
        "fix": f"fix-{repo_short}#{number}",
        "chore": f"chore-{repo_short}#{number}",
        "review": f"review-{repo_short}#{number}",
    }.get(issue_type, f"feat-{repo_short}#{number}")
    return f"{base}-cc" if agent == "cc" else f"{base}-{agent}" if agent == "oc" else base


def prompt_path_for(session_name: str) -> Path:
    return Path(PROMPT_DIR) / f"tmq-prompt-{session_name}.txt"
