"""Tests for src/tmq/events.py — provider/model provenance.

Mirrors tms#73's parameterized tests for _resolve_dispatch_model.
Tests are ported to the bogocat-tmq API surface (log_dispatch /
log_dispatch_failed) but assert the same resolution contract.

Database isolation: events.py writes to postgres via psycopg2.
Tests patch psycopg2.connect to capture the INSERT SQL + params
so we assert what the row would contain without needing a real DB.
"""

from unittest.mock import MagicMock, patch

import pytest

# ── Shared fixtures ───────────────────────────────────────────────


def _capture_insert(mock_connect):
    """Set up mock psycopg2 so we can capture the INSERT params."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn
    return mock_cursor


def _last_insert_params(mock_cursor):
    """Return the params tuple from the most recent execute() call."""
    call_args = mock_cursor.execute.call_args
    if call_args is None:
        return None
    return call_args[0][1]  # args[0] = (sql, params)


# ── Tests ─────────────────────────────────────────────────────────


class TestLogDispatchProviderResolution:
    """Provider/model provenance for log_dispatch (tms#83)."""

    def test_explicit_model_derives_provider(self):
        """A --model without --provider derives provider from the fleet map."""
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            from tmq.events import log_dispatch

            log_dispatch(
                repo="tms", issue=83, agent="pi",
                provider="", model="MiniMax-M3",
                dispatch_type="fix", cwd="/tmp/wt", session_name="fix-tms#83",
            )
            params = _last_insert_params(cursor)
            # provider column (index 7 in INSERT: id,created_at,event_ts,event_type,
            # repo,issue,agent,provider,model,...)
            assert params[7] == "minimax"
            assert params[8] == "MiniMax-M3"

    def test_explicit_provider_and_model_preserved(self):
        """Explicit provider + model passed through unchanged."""
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            from tmq.events import log_dispatch

            log_dispatch(
                repo="tms", issue=83, agent="pi",
                provider="custom-p", model="custom-m",
                dispatch_type="fix", cwd="/tmp/wt", session_name="fix-tms#83",
            )
            params = _last_insert_params(cursor)
            assert params[7] == "custom-p"
            assert params[8] == "custom-m"

    @pytest.mark.parametrize(
        ("model", "expected_provider"),
        [
            ("deepseek-v4-pro", "deepseek"),
            ("MiniMax-M3", "minimax"),
            ("MiniMax-M3.5", "minimax"),
            ("glm-5.2", "zai"),
        ],
    )
    def test_each_known_model_gets_correct_provider(self, model, expected_provider):
        """Every fleet model maps to its known provider (tms#73 ports)."""
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            from tmq.events import log_dispatch

            log_dispatch(
                repo="tms", issue=83, agent="pi",
                provider="", model=model,
                dispatch_type="fix", cwd="/tmp/wt", session_name="fix-tms#83",
            )
            params = _last_insert_params(cursor)
            assert params[7] == expected_provider
            assert params[8] == model

    def test_explicit_model_ignores_default_provider(self):
        """An explicit --model must NOT combine with a stale default provider.

        If the caller sets provider='stale-default' (from settings) but no
        explicit --model, defaults are consulted. But with an explicit model,
        the provider is derived from the model, not the stale default.
        """
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            from tmq.events import log_dispatch

            # Simulate: settings.pi_provider='stale-default' but args.model='MiniMax-M3'
            log_dispatch(
                repo="tms", issue=83, agent="pi",
                provider="stale-default", model="MiniMax-M3",
                dispatch_type="fix", cwd="/tmp/wt", session_name="fix-tms#83",
            )
            params = _last_insert_params(cursor)
            assert params[7] == "minimax", (
                "explicit model should override stale default provider"
            )
            assert params[8] == "MiniMax-M3"

    def test_empty_both_resolves_from_defaults(self):
        """Empty provider + model resolves from pi settings defaults."""
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            with patch("tmq.events._resolve_default_model",
                       return_value=("deepseek", "deepseek-v4-pro")):
                from tmq.events import log_dispatch

                log_dispatch(
                    repo="tms", issue=83, agent="pi",
                    provider="", model="",
                    dispatch_type="fix", cwd="/tmp/wt", session_name="fix-tms#83",
                )
                params = _last_insert_params(cursor)
                assert params[7] == "deepseek"
                assert params[8] == "deepseek-v4-pro"


class TestLogDispatchFailedProviderResolution:
    """Provider/model provenance for log_dispatch_failed (tms#83)."""

    def test_explicit_model_derives_provider_on_failure(self):
        """dispatch_failed must also resolve provider from model."""
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            from tmq.events import log_dispatch_failed

            log_dispatch_failed(
                repo="tms", issue=83, agent="pi",
                provider="", model="MiniMax-M3.5",
                dispatch_type="fix", reason="aoe add failed",
            )
            params = _last_insert_params(cursor)
            assert params[7] == "minimax"
            assert params[8] == "MiniMax-M3.5"

    def test_failed_uses_defaults_when_empty(self):
        """Failed dispatch with no model resolves from pi settings."""
        with patch("psycopg2.connect") as mock_connect:
            cursor = _capture_insert(mock_connect)
            with patch("tmq.events._resolve_default_model",
                       return_value=("zai", "glm-5.2")):
                from tmq.events import log_dispatch_failed

                log_dispatch_failed(
                    repo="tms", issue=83, agent="pi",
                    provider="", model="",
                    dispatch_type="fix", reason="timeout",
                )
                params = _last_insert_params(cursor)
                assert params[7] == "zai"
                assert params[8] == "glm-5.2"
