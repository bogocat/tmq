"""`tmq list --machine` — the tab-separated contract tms's review poller parses.

Regression guard for the silent failure this flag was added for: `tms`'s
`lib/tms/review_poll.py::_repo_registry` shells out to `tmq list --machine` and
splits each line on tabs into (short, path, gh, worktree). When the flag does
not exist, `--machine` is an argparse error, stdout is empty, the registry comes
back `[]`, and the poller prints "No open PRs need review" every five minutes
forever — a review gate that reports nothing to review because it cannot see
anything. See bogocat/tms for the consumer side.
"""

from __future__ import annotations

from tmq.cli import build_parser, format_machine_repos


REPOS = [
    {"short": "deploy", "path": "/root/deploy/distillery", "gh": "bogocat/distillery", "worktree": False},
    {"short": "distillery", "path": "/root/projects/distillery", "gh": "bogocat/distillery", "worktree": True},
    {"short": "tms", "path": "/root/tms", "gh": "bogocat/tms", "worktree": True},
]


class TestParserAcceptsTheFlag:
    """The flag must parse. This is the whole bug: it did not exist."""

    def test_list_machine_is_parseable(self):
        args = build_parser().parse_args(["list", "--machine"])
        assert args.first == "list"
        assert args.machine is True

    def test_machine_defaults_false(self):
        args = build_parser().parse_args(["list"])
        assert args.machine is False


class TestMachineRowContract:
    """Exactly what _repo_registry does with the output, applied to ours."""

    def test_four_tab_separated_fields(self):
        out = format_machine_repos(REPOS)
        lines = out.splitlines()
        assert len(lines) == len(REPOS)
        for line in lines:
            parts = line.split("\t")
            assert len(parts) == 4, f"expected 4 tab-separated fields, got {parts!r}"

    def test_worktree_is_1_or_0_and_parses_as_int(self):
        out = format_machine_repos(REPOS)
        flags = {line.split("\t")[0]: line.split("\t")[3] for line in out.splitlines()}
        assert flags == {"deploy": "0", "distillery": "1", "tms": "1"}
        # The consumer does int() on this field; anything else raises there.
        for v in flags.values():
            assert int(v) in (0, 1)

    def test_repo_registry_round_trip(self):
        """Reimplement _repo_registry's parse+dedupe against real output.

        Prefers worktree=1 per gh repo, so `deploy` loses to `distillery`.
        """
        rows = {}
        for line in format_machine_repos(REPOS).splitlines():
            parts = line.split("\t")
            assert len(parts) >= 4
            short, _path, gh, wt = parts[0], parts[1], parts[2], parts[3]
            rows.setdefault(gh, []).append((short, int(wt)))

        resolved = {}
        for gh, entries in rows.items():
            entries.sort(key=lambda e: (e[1] != 1, 0))
            resolved[gh] = entries[0][0]

        assert resolved == {
            "bogocat/distillery": "distillery",
            "bogocat/tms": "tms",
        }

    def test_empty_registry_emits_nothing(self):
        """Empty output is honest: the consumer then yields []. It must not
        emit a header line or a placeholder row that would parse as a repo."""
        assert format_machine_repos([]) == ""

    def test_missing_worktree_key_is_0(self):
        out = format_machine_repos([{"short": "x", "path": "/x", "gh": "g/x"}])
        assert out.split("\t")[3] == "0"
