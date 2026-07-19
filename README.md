# tmq

tmq — spawn a coding agent on a GitHub issue. Agent of Empires plugin + standalone CLI.

A [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires)
plugin (`bogocat.tmq`) and a backing `tmq` CLI that together replace the
930-line bash tool that lived in `bogocat/tms/bin/tmq`. Same dispatch
behavior, four new affordances:

- Discoverable via `aoe plugin info bogocat.tmq` and the AoE marketplace
- Invokable from the AoE command palette (TUI/web) under `Plugin: tmq`
- Live `pane` UI showing in-flight + recent dispatches (closes tms#35)
- One Python package, two `console_scripts`: `tmq-worker` (daemon) +
  `tmq` (CLI) — they share the same handlers, so the plugin path
  and the CLI path never diverge

## Install into Agent of Empires

```bash
# from a local checkout (builds the venv in .aoe-build/)
aoe plugin install ./tmq

# from GitHub once v0.1.0 is tagged
aoe plugin install gh:bogocat/tmq
```

The host runs the manifest's `[[runtime.build]]` steps at install time and
launches the plugin-relative worker entrypoint under `.aoe-build/`.
Manage the plugin from the TUI, the web dashboard, or
`aoe plugin {enable,disable,update}`.

## Install the CLI alone

`aoe plugin install ./` gives you both `tmq-worker` (via the host's daemon)
and `tmq` (only because `[project.scripts]` registers it). If you only want
the CLI — e.g. on an operator host without `aoe` yet:

```bash
pipx install /path/to/tmq       # exposes /usr/local/bin/tmq
# or, fleet-wide:
pip install --break-system-packages /path/to/tmq
```

The package source is the same directory whether installed through AoE or
through pip. Both code paths dispatch through `tmq.handlers` so a CLI
invocation and a plugin invocation do exactly the same work.

## Commands

The plugin contributes four commands (host-prefixed as
`plugin.bogocat.tmq.<id>`):

| command      | what it does |
|--------------|--------------|
| `dispatch`   | fetch issue, worktree, prompt, aoe add + start (or tmux fallback) |
| `review`     | same as `dispatch` but with `--type review`, resolves issue→PR |
| `list_repos` | dump the 21-repo registry (path, gh slug, worktree policy) |
| `status`     | runtime health + last 10 dispatch events |

The worker answers on the trailing segment so `bogocat.tmq.dispatch` and
`plugin.bogocat.tmq.dispatch` both resolve.

## Settings

Six plugin settings (read back by the worker via `config.get`):

| key | default | meaning |
|-----|---------|---------|
| `default_agent` | `pi` | `-agent` default for `tmq <repo> <n>` |
| `pi_provider`   | empty | forwarded to `pi --provider` |
| `pi_model`      | empty | forwarded to `pi --model` |
| `registry_path` | empty | override the shipped 21-repo JSON at `~/.config/tmq/registry.json` |
| `events_dsn`    | empty | DSN for `tms_review.events` (default: `service=bogocat`) |
| `allow_root_cc` | false | opt-in to Claude Code under root/sudo (tms#39 guard) |

## Layout

```
aoe-plugin.toml        the manifest (commands, settings, ui slots, runtime)
pyproject.toml         build + dev tooling (hatchling, ruff, mypy, pytest)
src/tmq/
  worker.py            the JSON-RPC worker (long-lived, ndjson, bi-directional)
  rpc.py               response builders (MethodNotFoundError -> -32601)
  handlers.py          the four command handlers (dispatch / review / list_repos / status)
  cli.py               argparse wrapper that calls the same handlers
  registry.py          the 21-repo table (data/default_registry.json + user overlay)
  gh.py                gh issue/pr fetch + PR-number resolution (tms#10 review P1)
  worktree.py          git worktree add / find / clean, feat/branch naming
  prompt.py            the AGENTS.md-style dispatch prompt builder
  spawn.py             aoe add + session start (with raw tmux fallback)
  events.py            tms_review.events writer (postgres, jsonb payload)
  ui_state.py          the tmq_pane payload
  data/default_registry.json   21-repo baseline (mirror of tms/bin/tmq)
tests/                 worker-contract, handlers, registry, worktree, gh, pane-push
```

## Chunking policy

Dispatched agents are instructed to decompose large PRs at the plan gate.
When an agent's plan projects a diff larger than **500 lines** (insertions +
deletions), the plan must include a **chunk plan** — an ordered sequence of
≤500-line sub-PRs with explicit dependencies — before the human approves.

### Example chunk plan

```
Projected diff: ~850 lines
Chunk plan:
  1. Add shared types and validation utilities        (~180 lines, independent)
  2. Implement core logic (stacks on PR-1)             (~300 lines, stacks on 1)
  3. Wire CLI entry point + integration tests          (~220 lines, stacks on 2)
  4. Update README + docs                              (~150 lines, independent)
```

### Stacked-PR safety

When chunks stack (e.g. PR-2 depends on PR-1's branch):

- **Merge in order** — merge PR-1 first, then PR-2, etc.
- **Never use `--delete-branch` on a stacked PR** — deleting a base branch
  closes every dependent PR.
- Delete branches only after the full stack lands on main.

### Rationale

Industry data shows AI review returns diminish sharply above 500 lines
(models fall back to surface pattern-matching). Our own evidence agrees:
tms#87 was +1210 lines and needed 6 review rounds including a FAIL.
Chunking at the plan gate keeps each review small enough to get a thorough
multi-model panel.

## Develop

This plugin uses [uv](https://docs.astral.sh/uv/) for the test loop. System
Python works for an editable install (`pip install -e .`).

```bash
uv sync                       # install deps into a local venv
uv run ruff check .           # lint
uv run ruff format --check .  # format check
uv run mypy src               # type-check
uv run pytest                 # test
```

Drive the worker by hand (it reads one JSON-RPC request per line on stdin):

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"bogocat.tmq.status","params":{}}' \
  | uv run tmq-worker
```

Try the CLI against the same registry the plugin uses:

```bash
uv run tmq list                # 21 repos
uv run tmq status              # last 10 dispatch events
uv run tmq distillery 245      # spawn pi on a real issue (creates /root/wt-...)
```

## Worker protocol

The host spawns the worker and exchanges newline-delimited JSON-RPC 2.0
messages over stdio. Method dispatch uses the trailing segment so the host
prefix is irrelevant. Errors:

| code   | meaning |
|--------|---------|
| `-32601` | Method not found |
| `-32603` | Internal error (Python exception escaped) |
| `-32001` | User input error (invalid repo, wrong agent, etc.) |
| `-32002` | External system error (`gh`/`git`/`aoe`/`tmux` failed) |

Notifications (no `id`) get no reply. On every successful dispatch / review
the worker pushes a `ui.state.set` notification to its `tmq_pane` slot
with the in-flight + recent-event state — visible in the AoE TUI/web
dashboard as soon as the host renders the pane.

See the [plugin authoring guide](https://agent-of-empires.com/docs/development/writing-plugins/)
and the [plugin API reference](https://agent-of-empires.com/docs/api/plugin-api/).

## Release

Push a `v0.1.0`-style tag to run the release workflow: it runs checks,
generates the changelog from conventional commits, and publishes a
GitHub Release with a source archive + sha256. To get listed in the
Agent of Empires featured index, see the authoring guide's
publishing section.

## License

MIT
