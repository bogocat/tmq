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


class TestMainWiresTheFlag:
    """The half of the bug the unit tests above cannot see.

    The original break was not only "no --machine in the parser" but
    "main() hardcoded format: human". A suite that tests the parser and the
    serializer separately stays green with that wiring reverted, which is how
    the poller stayed silently dead. These tests exercise main() itself.
    """

    def _fake_emit(self, seen):
        def fake(key, params, settings):
            seen["key"] = key
            seen["format"] = params["args"]["format"]
            return {"repos": REPOS, "text": "deploy  bogocat/distillery  in-place  /root/deploy/distillery"}

        return fake

    def test_machine_is_asked_for_and_emitted(self, monkeypatch, capsys):
        from tmq import cli

        seen: dict[str, str] = {}
        monkeypatch.setattr(cli, "_emit", self._fake_emit(seen))
        assert cli.main(["list", "--machine"]) == 0
        assert seen["key"] == "list_repos"
        assert seen["format"] == "machine", "main() must forward the flag to the handler"
        first = capsys.readouterr().out.splitlines()[0]
        assert len(first.split("\t")) == 4
        assert first.split("\t")[3] in {"0", "1"}

    def test_human_is_the_default_and_prints_the_text(self, monkeypatch, capsys):
        from tmq import cli

        seen: dict[str, str] = {}
        monkeypatch.setattr(cli, "_emit", self._fake_emit(seen))
        assert cli.main(["list"]) == 0
        assert seen["format"] == "human"
        # The human path prints the handler's text, never tab rows.
        out = capsys.readouterr().out
        assert "in-place" in out
        assert len(out.splitlines()[0].split("\t")) == 1

    def test_empty_registry_prints_nothing_at_all(self, monkeypatch, capsys):
        from tmq import cli

        monkeypatch.setattr(cli, "_emit", lambda key, params, settings: {"repos": []})
        assert cli.main(["list", "--machine"]) == 0
        assert capsys.readouterr().out == ""
