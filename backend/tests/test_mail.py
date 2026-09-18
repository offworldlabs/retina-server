"""Unit tests for the outbound mail helper in services/mail.py.

The module reads MAIL_TRANSPORT, MAIL_FROM, CLOUDFLARE_EMAIL_TOKEN and
RETINA_ENV from the environment on each call rather than once at import, for
the reason services/alerting.py gives: main.py calls load_dotenv() after its
service imports, so an import-time read sees an empty environment on a start
that does not already carry the variables. These tests therefore drive it
through monkeypatch.setenv/delenv rather than by patching module attributes.
"""

import logging
import smtplib
from unittest.mock import MagicMock, patch

import pytest

import services.mail as _mail
from services.mail import is_configured, send, transport


@pytest.fixture(autouse=True)
def _clean_mail_env(monkeypatch):
    """Start every test with all settings unset, whatever the ambient shell or
    .env holds, so each test's setenv/delenv calls are the only source of truth
    for what the module sees. RETINA_ENV is included because conftest sets it
    for the whole session and the production refusal keys off it."""
    for key in ("MAIL_TRANSPORT", "MAIL_FROM", "CLOUDFLARE_EMAIL_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RETINA_ENV", "test")


@pytest.fixture()
def smtp_configured(monkeypatch):
    """A complete SMTP configuration, for tests about behaviour rather than
    about which settings are required."""
    monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "token-value-not-in-logs")


# ── Resolving the transport ───────────────────────────────────────────────────


class TestTransport:
    def test_unset_is_unconfigured(self):
        assert transport() == ""
        assert is_configured() is False

    def test_empty_string_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MAIL_TRANSPORT", "")
        assert transport() == ""

    def test_whitespace_only_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MAIL_TRANSPORT", "   ")
        assert transport() == ""

    def test_unset_does_not_warn(self, monkeypatch, caplog):
        """Not configuring mail is a choice, not a mistake. Only a value the
        module cannot honour is worth a warning."""
        with caplog.at_level(logging.WARNING):
            transport()
        assert caplog.records == []

    def test_unknown_value_warns_and_is_unconfigured(self, monkeypatch, caplog):
        monkeypatch.setenv("MAIL_TRANSPORT", "sendgrid")
        with caplog.at_level(logging.WARNING):
            assert transport() == ""
        assert "sendgrid" in caplog.text

    def test_value_is_case_insensitive_and_trimmed(self, monkeypatch):
        monkeypatch.setenv("MAIL_TRANSPORT", "  SMTP  ")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "t")
        assert transport() == "smtp"


class TestSmtpRequiresItsSettings:
    def test_missing_token_is_unconfigured(self, monkeypatch, caplog):
        monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with caplog.at_level(logging.WARNING):
            assert transport() == ""
        assert "CLOUDFLARE_EMAIL_TOKEN" in caplog.text

    def test_missing_from_is_unconfigured(self, monkeypatch, caplog):
        monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
        monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "t")
        with caplog.at_level(logging.WARNING):
            assert transport() == ""
        assert "MAIL_FROM" in caplog.text

    def test_complete_configuration_resolves(self, smtp_configured):
        assert transport() == "smtp"
        assert is_configured() is True


class TestLogTransport:
    def test_needs_no_token(self, monkeypatch):
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        assert transport() == "log"

    def test_refused_in_production(self, monkeypatch, caplog):
        """A transport that writes credentials to a log file instead of
        delivering them must not resolve in production, whatever is set."""
        monkeypatch.setenv("RETINA_ENV", "production")
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with caplog.at_level(logging.WARNING):
            assert transport() == ""
        assert "production" in caplog.text.lower()

    def test_refusal_leaves_mail_unconfigured_rather_than_falling_back(self, monkeypatch):
        """Fails closed: production with MAIL_TRANSPORT=log sends nothing, it
        does not quietly become smtp."""
        monkeypatch.setenv("RETINA_ENV", "production")
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "t")
        assert is_configured() is False


# ── Sending ───────────────────────────────────────────────────────────────────


class TestSend:
    def test_unconfigured_returns_false_and_does_not_raise(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert send("owner@example.com", "Subject", "Body") is False

    def test_log_transport_writes_the_body(self, monkeypatch, caplog):
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with caplog.at_level(logging.INFO):
            assert send("owner@example.com", "Sign in", "https://app.retina.fm/x") is True
        assert "owner@example.com" in caplog.text
        assert "https://app.retina.fm/x" in caplog.text

    def test_smtp_connects_to_cloudflare_with_implicit_tls(self, smtp_configured):
        with patch.object(_mail.smtplib, "SMTP_SSL") as ssl:
            assert send("owner@example.com", "Sign in", "Body") is True
        ssl.assert_called_once()
        assert ssl.call_args.args[0] == "smtp.mx.cloudflare.net"
        assert ssl.call_args.args[1] == 465

    def test_smtp_logs_in_with_the_literal_api_token_username(self, smtp_configured):
        with patch.object(_mail.smtplib, "SMTP_SSL") as ssl:
            send("owner@example.com", "Sign in", "Body")
        server = ssl.return_value.__enter__.return_value
        server.login.assert_called_once_with("api_token", "token-value-not-in-logs")

    def test_smtp_sends_the_message_with_correct_headers(self, smtp_configured):
        with patch.object(_mail.smtplib, "SMTP_SSL") as ssl:
            send("owner@example.com", "Sign in to RETINA", "Your link")
        server = ssl.return_value.__enter__.return_value
        server.send_message.assert_called_once()
        message = server.send_message.call_args.args[0]
        assert message["To"] == "owner@example.com"
        assert message["From"] == "RETINA <no-reply@retina.fm>"
        assert message["Subject"] == "Sign in to RETINA"
        assert "Your link" in message.get_content()

    def test_a_long_url_survives_the_encoding_unbroken(self, smtp_configured):
        """A sign-in URL can run past the 78 columns that select quoted-printable
        (staging's does), and QP splits it with a soft `=`. A compliant client
        rejoins it, but a plain-text view or a copy-paste hands the reader half a
        token."""
        url = "https://staging-app.retina.fm/auth/link/" + "t" * 43
        assert len(url) > 78
        with patch.object(_mail.smtplib, "SMTP_SSL") as ssl:
            send("owner@example.com", "Sign in", f"Click:\n\n{url}\n")
        message = ssl.return_value.__enter__.return_value.send_message.call_args.args[0]
        assert url in message.as_string()

    def test_a_non_ascii_body_still_sends(self, smtp_configured):
        """The 7bit encoding above is only valid for ASCII, so anything else
        has to fall back rather than raise out of send()."""
        with patch.object(_mail.smtplib, "SMTP_SSL") as ssl:
            assert send("owner@example.com", "Sign in", "Ünicode café ✈") is True
        message = ssl.return_value.__enter__.return_value.send_message.call_args.args[0]
        assert "café" in message.get_content()

    def test_smtp_failure_is_logged_and_returns_false(self, smtp_configured, caplog):
        """A send that fails must not reach the caller as an exception: the
        magic-link route answers the same way whether or not mail went out, and
        an exception there would make the response an enumeration oracle."""
        with patch.object(_mail.smtplib, "SMTP_SSL", side_effect=smtplib.SMTPException("nope")):
            with caplog.at_level(logging.ERROR):
                assert send("owner@example.com", "Sign in", "Body") is False
        assert "owner@example.com" in caplog.text

    def test_transport_error_is_logged_and_returns_false(self, smtp_configured):
        with patch.object(_mail.smtplib, "SMTP_SSL", side_effect=OSError("unreachable")):
            assert send("owner@example.com", "Sign in", "Body") is False

    @pytest.mark.parametrize("field", ["to", "subject"])
    def test_a_linefeed_in_a_header_is_refused_without_raising(self, smtp_configured, field, caplog):
        """EmailMessage's default policy rejects a header value carrying a
        linefeed, which is what stops "a@b.com\\nBcc: attacker@evil.com" adding
        a recipient. It rejects by raising, so this must be caught: a hostile
        address answering differently from an unknown one is the enumeration
        oracle send() exists to avoid."""
        hostile = "someone@example.com\nBcc: attacker@evil.com"
        to = hostile if field == "to" else "owner@example.com"
        subject = hostile if field == "subject" else "Sign in"
        with patch.object(_mail.smtplib, "SMTP_SSL") as ssl:
            with caplog.at_level(logging.ERROR):
                assert send(to, subject, "Body") is False
        ssl.return_value.__enter__.return_value.send_message.assert_not_called()

    def test_token_never_appears_in_logs_on_failure(self, smtp_configured, caplog):
        with patch.object(_mail.smtplib, "SMTP_SSL", side_effect=smtplib.SMTPException("nope")):
            with caplog.at_level(logging.DEBUG):
                send("owner@example.com", "Sign in", "Body")
        assert "token-value-not-in-logs" not in caplog.text


class TestSendInBackground:
    @pytest.fixture(autouse=True)
    def _clear_inflight(self):
        """The in-flight count is module state, so a test that leaves it raised
        would silently cap every test after it."""
        _mail._inflight = 0
        yield
        _mail._inflight = 0

    def test_drops_the_send_once_the_cap_is_reached(self, smtp_configured, caplog):
        _mail._inflight = _mail._MAX_INFLIGHT_SENDS
        with patch.object(_mail.threading, "Thread") as thread:
            with caplog.at_level(logging.ERROR):
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
        thread.assert_not_called()
        assert "owner@example.com" in caplog.text

    def test_the_slot_is_released_when_delivery_finishes(self, smtp_configured):
        with patch.object(_mail, "send", return_value=True):
            with patch.object(_mail.threading, "Thread") as thread:
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
            assert _mail._inflight == 1
            thread.call_args.kwargs["target"]()
        assert _mail._inflight == 0

    def test_the_slot_is_released_when_delivery_raises(self, smtp_configured):
        with patch.object(_mail, "send", side_effect=RuntimeError("boom")):
            with patch.object(_mail.threading, "Thread") as thread:
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
            with pytest.raises(RuntimeError):
                thread.call_args.kwargs["target"]()
        assert _mail._inflight == 0

    def test_a_thread_that_never_started_gives_its_slot_back(self, smtp_configured):
        """Otherwise the cap ratchets down to zero and mail stops for good."""
        with patch.object(_mail.threading, "Thread", side_effect=RuntimeError("no threads")):
            _mail.send_in_background("owner@example.com", "Sign in", "Body")
        assert _mail._inflight == 0

    def test_returns_without_waiting_for_delivery(self, smtp_configured):
        """The caller answers identically for a known and an unknown address,
        so it must not wait on a send whose duration depends on the address."""
        started = MagicMock()
        with patch.object(_mail.threading, "Thread", return_value=started) as thread:
            _mail.send_in_background("owner@example.com", "Sign in", "Body")
        thread.assert_called_once()
        assert thread.call_args.kwargs["daemon"] is True
        started.start.assert_called_once()

    def test_thread_creation_failure_does_not_reach_the_caller(self, smtp_configured, caplog):
        with patch.object(_mail.threading, "Thread", side_effect=RuntimeError("no threads")):
            with caplog.at_level(logging.ERROR):
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
        assert "no threads" in caplog.text

    def test_delivery_runs_on_the_thread(self, smtp_configured):
        with patch.object(_mail, "send", return_value=True) as delivered:
            with patch.object(_mail.threading, "Thread") as thread:
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
            thread.call_args.kwargs["target"]()
        delivered.assert_called_once_with("owner@example.com", "Sign in", "Body")


class TestLogDestination:
    def test_says_nothing_when_unconfigured(self, caplog):
        with caplog.at_level(logging.INFO):
            _mail.log_destination()
        assert caplog.records == []

    def test_names_the_transport_and_sender(self, smtp_configured, caplog):
        with caplog.at_level(logging.INFO):
            _mail.log_destination()
        assert "smtp" in caplog.text
        assert "no-reply@retina.fm" in caplog.text

    def test_never_prints_the_token(self, smtp_configured, caplog):
        with caplog.at_level(logging.DEBUG):
            _mail.log_destination()
        assert "token-value-not-in-logs" not in caplog.text

    def test_log_transport_is_announced_loudly(self, monkeypatch, caplog):
        """Mail going to a log file rather than a mailbox is worth a WARNING,
        so it shows up in a deploy's own logs and not only in a terminal."""
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with caplog.at_level(logging.INFO):
            _mail.log_destination()
        assert any(r.levelno == logging.WARNING for r in caplog.records)
