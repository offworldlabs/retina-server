"""Unit tests for the outbound mail helper in services/mail.py.

The module reads MAIL_TRANSPORT, MAIL_FROM, CLOUDFLARE_EMAIL_TOKEN,
CLOUDFLARE_ACCOUNT_ID and RETINA_ENV from the environment on each call rather than once at import, for
the reason services/alerting.py gives: main.py calls load_dotenv() after its
service imports, so an import-time read sees an empty environment on a start
that does not already carry the variables. These tests therefore drive it
through monkeypatch.setenv/delenv rather than by patching module attributes.
"""

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

import services.mail as _mail
from services.mail import is_configured, send, transport


@pytest.fixture(autouse=True)
def _clean_mail_env(monkeypatch):
    """Start every test with all settings unset, whatever the ambient shell or
    .env holds, so each test's setenv/delenv calls are the only source of truth
    for what the module sees. RETINA_ENV is included because conftest sets it
    for the whole session and the production refusal keys off it."""
    for key in ("MAIL_TRANSPORT", "MAIL_FROM", "CLOUDFLARE_EMAIL_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RETINA_ENV", "test")


@pytest.fixture()
def cloudflare_configured(monkeypatch):
    """A complete Cloudflare configuration, for tests about behaviour rather
    than about which settings are required."""
    monkeypatch.setenv("MAIL_TRANSPORT", "cloudflare")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "token-value-not-in-logs")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")


SEND_URL = "https://api.cloudflare.com/client/v4/accounts/acct123/email/sending/send"


def _answer(status: int = 200, payload=None, text: str | None = None) -> httpx.Response:
    """A response as Cloudflare's send endpoint gives one. Accepted for queueing
    unless told otherwise, which is what a real send to an outside mailbox
    returned."""
    if payload is None and text is None:
        payload = {
            "success": True,
            "errors": [],
            "result": {
                "message_id": "<m@retina.fm>",
                "delivered": [],
                "queued": ["owner@example.com"],
                "permanent_bounces": [],
                "suppressed_recipients": [],
            },
        }
    request = httpx.Request("POST", SEND_URL)
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, json=payload, request=request)


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

    def test_value_is_case_insensitive_and_trimmed(self, cloudflare_configured, monkeypatch):
        monkeypatch.setenv("MAIL_TRANSPORT", "  CLOUDFLARE  ")
        assert transport() == "cloudflare"


class TestCloudflareRequiresItsSettings:
    @pytest.mark.parametrize("missing", ["CLOUDFLARE_EMAIL_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "MAIL_FROM"])
    def test_each_missing_setting_is_unconfigured_and_named(self, cloudflare_configured, monkeypatch, caplog, missing):
        monkeypatch.delenv(missing)
        with caplog.at_level(logging.WARNING):
            assert transport() == ""
        assert missing in caplog.text

    def test_complete_configuration_resolves(self, cloudflare_configured):
        assert transport() == "cloudflare"
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
        does not quietly become cloudflare."""
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

    def test_log_transport_keeps_a_long_url_unbroken(self, monkeypatch, caplog):
        """A sign-in URL can run past the 78 columns that select quoted-printable
        (staging's does), and QP splits it with a soft `=`. The log is read by a
        person copying the link out, who would get half a token."""
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        url = "https://staging-app.retina.fm/auth/link/" + "t" * 43
        assert len(url) > 78
        with caplog.at_level(logging.INFO):
            send("owner@example.com", "Sign in", f"Click:\n\n{url}\n")
        assert url in caplog.text

    def test_posts_to_the_accounts_send_endpoint(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            assert send("owner@example.com", "Sign in", "Body") is True
        post.assert_called_once()
        assert post.call_args.args[0] == SEND_URL

    def test_authenticates_with_the_token_as_a_bearer(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            send("owner@example.com", "Sign in", "Body")
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer token-value-not-in-logs"

    def test_sends_sender_recipient_subject_and_body(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            send("owner@example.com", "Sign in to RETINA", "Your link")
        assert post.call_args.kwargs["json"] == {
            "from": {"address": "no-reply@retina.fm", "name": "RETINA"},
            "to": ["owner@example.com"],
            "subject": "Sign in to RETINA",
            "text": "Your link",
        }

    def test_a_bare_sender_address_is_sent_without_a_name(self, cloudflare_configured, monkeypatch):
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            send("owner@example.com", "Sign in", "Body")
        assert post.call_args.kwargs["json"]["from"] == {"address": "no-reply@retina.fm"}

    def test_the_request_has_a_timeout(self, cloudflare_configured):
        """send() runs on a daemon thread holding one of a capped number of
        slots, so a request that never returns would leak the slot for good."""
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            send("owner@example.com", "Sign in", "Body")
        assert post.call_args.kwargs["timeout"] > 0

    def test_a_non_ascii_body_still_sends(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            assert send("owner@example.com", "Sign in", "Ünicode café ✈") is True
        assert post.call_args.kwargs["json"]["text"] == "Ünicode café ✈"

    @pytest.mark.parametrize("outcome", ["permanent_bounces", "suppressed_recipients"])
    def test_an_undeliverable_recipient_is_a_failure(self, cloudflare_configured, caplog, outcome):
        """Cloudflare answers 200 for these, so success is not the status code."""
        result = {"delivered": [], "queued": [], "permanent_bounces": [], "suppressed_recipients": []}
        result[outcome] = ["owner@example.com"]
        answer = _answer(payload={"success": True, "errors": [], "result": result})
        with patch.object(_mail.httpx, "post", return_value=answer):
            with caplog.at_level(logging.ERROR):
                assert send("owner@example.com", "Sign in", "Body") is False
        assert "owner@example.com" in caplog.text
        assert outcome.split("_")[0] in caplog.text

    def test_an_error_response_is_logged_with_cloudflares_reason(self, cloudflare_configured, caplog):
        answer = _answer(403, {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]})
        with patch.object(_mail.httpx, "post", return_value=answer):
            with caplog.at_level(logging.ERROR):
                assert send("owner@example.com", "Sign in", "Body") is False
        assert "403" in caplog.text
        assert "10000 Authentication error" in caplog.text

    def test_an_error_response_that_is_not_json_does_not_raise(self, cloudflare_configured, caplog):
        with patch.object(_mail.httpx, "post", return_value=_answer(502, text="<html>Bad gateway</html>")):
            with caplog.at_level(logging.ERROR):
                assert send("owner@example.com", "Sign in", "Body") is False
        assert "502" in caplog.text

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectError("unreachable"), httpx.ReadTimeout("timed out")],
        ids=["unreachable", "timeout"],
    )
    def test_a_network_failure_is_logged_and_returns_false(self, cloudflare_configured, caplog, error):
        """A send that fails must not reach the caller as an exception: the
        magic-link route answers the same way whether or not mail went out, and
        an exception there would make the response an enumeration oracle."""
        with patch.object(_mail.httpx, "post", side_effect=error):
            with caplog.at_level(logging.ERROR):
                assert send("owner@example.com", "Sign in", "Body") is False
        assert "owner@example.com" in caplog.text

    @pytest.mark.parametrize("field", ["to", "subject"])
    def test_a_linefeed_in_a_header_is_refused_without_raising(self, cloudflare_configured, field, caplog):
        """EmailMessage's default policy rejects a header value carrying a
        linefeed, which is what stops "a@b.com\\nBcc: attacker@evil.com" adding
        a recipient. It rejects by raising, so this must be caught: a hostile
        address answering differently from an unknown one is the enumeration
        oracle send() exists to avoid."""
        hostile = "someone@example.com\nBcc: attacker@evil.com"
        to = hostile if field == "to" else "owner@example.com"
        subject = hostile if field == "subject" else "Sign in"
        with patch.object(_mail.httpx, "post") as post:
            with caplog.at_level(logging.ERROR):
                assert send(to, subject, "Body") is False
        post.assert_not_called()

    @pytest.mark.parametrize(
        "failure",
        [
            {"return_value": _answer(403, {"success": False, "errors": [{"code": 10000, "message": "nope"}]})},
            {"side_effect": httpx.ConnectError("nope")},
        ],
        ids=["error-response", "network"],
    )
    def test_token_never_appears_in_logs_on_failure(self, cloudflare_configured, caplog, failure):
        with patch.object(_mail.httpx, "post", **failure):
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

    def test_drops_the_send_once_the_cap_is_reached(self, cloudflare_configured, caplog):
        _mail._inflight = _mail._MAX_INFLIGHT_SENDS
        with patch.object(_mail.threading, "Thread") as thread:
            with caplog.at_level(logging.ERROR):
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
        thread.assert_not_called()
        assert "owner@example.com" in caplog.text

    def test_the_slot_is_released_when_delivery_finishes(self, cloudflare_configured):
        with patch.object(_mail, "send", return_value=True):
            with patch.object(_mail.threading, "Thread") as thread:
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
            assert _mail._inflight == 1
            thread.call_args.kwargs["target"]()
        assert _mail._inflight == 0

    def test_the_slot_is_released_when_delivery_raises(self, cloudflare_configured):
        with patch.object(_mail, "send", side_effect=RuntimeError("boom")):
            with patch.object(_mail.threading, "Thread") as thread:
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
            with pytest.raises(RuntimeError):
                thread.call_args.kwargs["target"]()
        assert _mail._inflight == 0

    def test_a_thread_that_never_started_gives_its_slot_back(self, cloudflare_configured):
        """Otherwise the cap ratchets down to zero and mail stops for good."""
        with patch.object(_mail.threading, "Thread", side_effect=RuntimeError("no threads")):
            _mail.send_in_background("owner@example.com", "Sign in", "Body")
        assert _mail._inflight == 0

    def test_returns_without_waiting_for_delivery(self, cloudflare_configured):
        """The caller answers identically for a known and an unknown address,
        so it must not wait on a send whose duration depends on the address."""
        started = MagicMock()
        with patch.object(_mail.threading, "Thread", return_value=started) as thread:
            _mail.send_in_background("owner@example.com", "Sign in", "Body")
        thread.assert_called_once()
        assert thread.call_args.kwargs["daemon"] is True
        started.start.assert_called_once()

    def test_thread_creation_failure_does_not_reach_the_caller(self, cloudflare_configured, caplog):
        with patch.object(_mail.threading, "Thread", side_effect=RuntimeError("no threads")):
            with caplog.at_level(logging.ERROR):
                _mail.send_in_background("owner@example.com", "Sign in", "Body")
        assert "no threads" in caplog.text

    def test_delivery_runs_on_the_thread(self, cloudflare_configured):
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

    def test_names_the_transport_and_sender(self, cloudflare_configured, caplog):
        with caplog.at_level(logging.INFO):
            _mail.log_destination()
        assert "cloudflare" in caplog.text
        assert "no-reply@retina.fm" in caplog.text

    def test_never_prints_the_token(self, cloudflare_configured, caplog):
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
