"""Unit tests for the outbound mail helper in services/mail.py.

The module reads MAIL_TRANSPORT, MAIL_FROM, CLOUDFLARE_EMAIL_TOKEN,
CLOUDFLARE_ACCOUNT_ID and RETINA_ENV from the environment on each call rather than once at import, for
the reason services/alerting.py gives: main.py calls load_dotenv() after its
service imports, so an import-time read sees an empty environment on a start
that does not already carry the variables. These tests therefore drive it
through monkeypatch.setenv/delenv rather than by patching module attributes.
"""

import email
import email.message
import email.policy
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import patch

import httpx
import pytest

import services.mail as _mail
from services import mail_templates
from services.mail import (
    REJECTED,
    RETRY,
    SENT,
    UNDELIVERABLE,
    Delivery,
    Message,
    deliver,
    is_configured,
    transport,
)


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


@pytest.fixture()
def log_configured(monkeypatch):
    monkeypatch.setenv("MAIL_TRANSPORT", "log")
    monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")


SEND_URL = "https://api.cloudflare.com/client/v4/accounts/acct123/email/sending/send"

#: Past the 78 columns that select quoted-printable, as staging's links are.
LONG_LINK = "https://staging-app.retina.fm/auth/link/" + "t" * 43 + "?next=/nodes/nde1a2b3c4d00"


def _message(to="owner@example.com", subject="Sign in", text="Body", html="<p>Body</p>") -> Message:
    return Message(to=to, subject=subject, text=text, html=html)


def _answer(status: int = 200, payload=None, text: str | None = None, headers=None) -> httpx.Response:
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
        return httpx.Response(status, text=text, headers=headers, request=request)
    return httpx.Response(status, json=payload, headers=headers, request=request)


def _error(status: int, code: int, message: str = "email.sending.error") -> httpx.Response:
    return _answer(status, {"success": False, "errors": [{"code": code, "message": message}], "result": None})


#: The two answers whose numeric code Cloudflare does not publish. Its schema
#: types `code` as nullable, so these are known by their message alone.
SUPPRESSED = "email.sending.error.email.recipient_suppressed"
NOT_READY = "email.sending.error.email.sending_not_ready"


def _refusal(status: int, *errors: dict, headers=None) -> httpx.Response:
    """A failed send carrying `errors` as given, in the shape of the API's 4xx schemas."""
    payload = {"success": False, "errors": list(errors), "messages": [], "result": None}
    return _answer(status, payload, headers=headers)


def _parts(raw: str) -> list[email.message.EmailMessage]:
    parsed = email.message_from_string(raw, policy=email.policy.default)
    assert isinstance(parsed, email.message.EmailMessage)
    return list(parsed.iter_parts())


def test_the_outcomes_are_the_strings_the_outbox_stores():
    assert (SENT, RETRY, REJECTED, UNDELIVERABLE) == ("sent", "retry", "rejected", "undeliverable")


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
    def test_needs_no_token(self, log_configured):
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


# ── Delivering without a transport ────────────────────────────────────────────


class TestUnconfigured:
    def test_is_worth_retrying_and_does_not_raise(self, caplog):
        """Configuration can arrive while the link is still good, so this is
        not a verdict on the message."""
        with patch.object(_mail.httpx, "post") as post:
            with caplog.at_level(logging.WARNING):
                delivery = deliver(_message())
        assert delivery.outcome == RETRY
        assert "not configured" in delivery.detail
        assert "owner@example.com" in caplog.text
        post.assert_not_called()

    def test_log_transport_in_production_writes_nothing(self, monkeypatch, caplog):
        monkeypatch.setenv("RETINA_ENV", "production")
        monkeypatch.setenv("MAIL_TRANSPORT", "log")
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with caplog.at_level(logging.DEBUG):
            delivery = deliver(_message(text=f"Click:\n{LONG_LINK}\n"))
        assert delivery.outcome == RETRY
        assert LONG_LINK not in caplog.text
        assert "MAIL_TRANSPORT=log, not sent" not in caplog.text

    def test_a_message_that_can_never_go_is_refused_rather_than_held(self):
        """Whether a message can go is a property of the message, so a bad
        one is refused now rather than retried until it is refused anyway."""
        assert deliver(_message(to="not-an-address")).outcome == REJECTED


# ── The MIME message ──────────────────────────────────────────────────────────


class TestMime:
    @pytest.fixture(autouse=True)
    def _sender(self, monkeypatch):
        monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")

    @staticmethod
    def _rendered() -> Message:
        rendered = mail_templates.sign_in(LONG_LINK)
        return Message("owner@example.com", rendered.subject, rendered.text, rendered.html)

    def test_is_multipart_alternative_with_the_text_first(self):
        """Parts run from least to most preferred, so a client that can show
        HTML shows the HTML."""
        mime = _mail._build(self._rendered())
        assert mime.get_content_type() == "multipart/alternative"
        assert [part.get_content_type() for part in mime.iter_parts()] == ["text/plain", "text/html"]

    def test_both_parts_declare_utf8(self):
        mime = _mail._build(self._rendered())
        assert [part.get_content_charset() for part in mime.iter_parts()] == ["utf-8", "utf-8"]

    def test_ascii_parts_go_7bit(self):
        mime = _mail._build(self._rendered())
        assert [part["Content-Transfer-Encoding"] for part in mime.iter_parts()] == ["7bit", "7bit"]

    def test_the_link_is_unbroken_in_both_parts_as_written(self):
        """Quoted-printable would split the link with a soft `=`, and a
        plain-text view or a copy-paste would then hand over half a token. The
        check is on the encoded payload, which is what a reader sees."""
        assert len(LONG_LINK) > 78
        raw = _mail._build(self._rendered()).as_bytes().decode("ascii")
        text, markup = _parts(raw)
        assert LONG_LINK in text.get_payload()
        assert f'href="{LONG_LINK}"' in markup.get_payload()

    def test_headers_carry_sender_recipient_and_subject(self):
        mime = _mail._build(self._rendered())
        assert mime["From"] == "RETINA <no-reply@retina.fm>"
        assert mime["To"] == "owner@example.com"
        assert mime["Subject"] == "Sign in to RETINA"

    def test_a_non_ascii_text_goes_8bit_and_keeps_its_link_whole(self):
        mime = _mail._build(_message(text=f"Ünicode café ✈ {LONG_LINK}\n"))
        text, markup = mime.iter_parts()
        assert text["Content-Transfer-Encoding"] == "8bit"
        assert markup["Content-Transfer-Encoding"] == "7bit"
        assert f"Ünicode café ✈ {LONG_LINK}".encode() in mime.as_bytes()

    def test_a_line_past_998_octets_is_left_to_the_library(self):
        """7bit and 8bit both cap a line at 998 octets, so past that the
        library's own choice is the only valid one."""
        mime = _mail._build(_message(text="x" * 1200 + "\n", html="<p>" + "y" * 1200 + "</p>\n"))
        assert [part["Content-Transfer-Encoding"] for part in mime.iter_parts()] == [
            "quoted-printable",
            "quoted-printable",
        ]
        assert max(len(line) for line in mime.as_bytes().splitlines()) <= 998


# ── The log transport ─────────────────────────────────────────────────────────


class TestLogTransportDelivery:
    def test_writes_the_whole_message_and_reports_it_sent(self, log_configured, caplog):
        with caplog.at_level(logging.INFO):
            delivery = deliver(_message(subject="Sign in", text="Plain body", html="<p>Rich body</p>"))
        assert delivery == Delivery(SENT, "written to the log")
        [record] = [r for r in caplog.records if r.levelno == logging.INFO]
        logged = record.getMessage()
        assert logged.startswith("MAIL_TRANSPORT=log, not sent:\n")
        for expected in ("To: owner@example.com", "Subject: Sign in", "multipart/alternative", "Plain body"):
            assert expected in logged
        assert "<p>Rich body</p>" in logged

    def test_keeps_a_long_url_unbroken_in_both_parts(self, log_configured, caplog):
        """A sign-in URL can run past the 78 columns that select quoted-printable
        (staging's does), and QP splits it with a soft `=`. The log is read by a
        person copying the link out, who would get half a token."""
        rendered = mail_templates.sign_in(LONG_LINK)
        with caplog.at_level(logging.INFO):
            deliver(Message("owner@example.com", rendered.subject, rendered.text, rendered.html))
        [record] = [r for r in caplog.records if r.levelno == logging.INFO]
        text, markup = _parts(record.getMessage().split("\n", 1)[1])
        assert LONG_LINK in text.get_payload()
        assert LONG_LINK in markup.get_payload()

    def test_a_non_ascii_body_is_written_readably(self, log_configured, caplog):
        """as_string() would turn an 8bit part into base64, which nobody can
        read the link out of."""
        with caplog.at_level(logging.INFO):
            assert deliver(_message(text=f"Ünicode café ✈\n{LONG_LINK}\n")).outcome == SENT
        assert "Ünicode café ✈" in caplog.text
        assert LONG_LINK in caplog.text


# ── The Cloudflare request ────────────────────────────────────────────────────


class TestCloudflareRequest:
    def test_posts_to_the_accounts_send_endpoint(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            assert deliver(_message()).outcome == SENT
        post.assert_called_once()
        assert post.call_args.args[0] == SEND_URL

    def test_authenticates_with_the_token_as_a_bearer(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            deliver(_message())
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer token-value-not-in-logs"

    def test_sends_sender_recipient_subject_text_and_html(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            deliver(_message(subject="Sign in to RETINA", text="Your link", html="<p>Your link</p>"))
        assert post.call_args.kwargs["json"] == {
            "from": {"address": "no-reply@retina.fm", "name": "RETINA"},
            "to": ["owner@example.com"],
            "subject": "Sign in to RETINA",
            "text": "Your link",
            "html": "<p>Your link</p>",
        }

    def test_a_bare_sender_address_is_sent_without_a_name(self, cloudflare_configured, monkeypatch):
        monkeypatch.setenv("MAIL_FROM", "no-reply@retina.fm")
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            deliver(_message())
        assert post.call_args.kwargs["json"]["from"] == {"address": "no-reply@retina.fm"}

    def test_the_request_has_a_timeout(self, cloudflare_configured):
        """The outbox runs deliver() on a worker thread, so a request that
        never returned would hold the thread, and the message, for good."""
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            deliver(_message())
        assert post.call_args.kwargs["timeout"] > 0

    def test_a_non_ascii_body_still_sends(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()) as post:
            assert deliver(_message(text="Ünicode café ✈", html="<p>Ünicode café ✈</p>")).outcome == SENT
        assert post.call_args.kwargs["json"]["text"] == "Ünicode café ✈"
        assert post.call_args.kwargs["json"]["html"] == "<p>Ünicode café ✈</p>"

    @pytest.mark.parametrize(
        "answer",
        [
            {"return_value": _answer()},
            {"return_value": _error(400, 10202)},
            {"return_value": _refusal(400, {"code": None, "message": SUPPRESSED})},
            {"return_value": _refusal(409, {"code": None, "message": NOT_READY})},
            {"side_effect": httpx.ConnectError("x")},
        ],
        ids=["sent", "refused", "suppressed", "not-ready", "network"],
    )
    def test_the_body_never_reaches_the_log_or_the_detail(self, cloudflare_configured, caplog, answer):
        """The body is the link, and the link is the credential."""
        with patch.object(_mail.httpx, "post", **answer):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message(text=f"Click:\n{LONG_LINK}\n", html=f'<a href="{LONG_LINK}">x</a>'))
        assert LONG_LINK not in caplog.text
        assert LONG_LINK not in delivery.detail


# ── What an answer means ──────────────────────────────────────────────────────


#: Cloudflare's documented error table (Email Sending REST API, "Error codes"),
#: plus the statuses around it that something in between could answer with.
CLASSIFICATION = [
    (400, 10001, REJECTED),  # invalid request schema
    (400, 10200, REJECTED),  # too big
    (400, 10201, REJECTED),  # no content length
    (400, 10202, REJECTED),  # invalid content
    (401, 10101, RETRY),  # missing or invalid token
    (401, 10103, RETRY),  # wrong token type
    (403, 10102, RETRY),  # token lacks permission
    (403, 10105, RETRY),  # account not entitled
    (403, 10203, RETRY),  # sending disabled
    (404, 10000, RETRY),  # not found: the account in the URL
    (422, 10001, REJECTED),  # any other 4xx
    (408, 10002, RETRY),  # the server's own timeout
    (429, 10004, RETRY),  # throttled
    (500, 10002, RETRY),  # internal error
    (500, 10003, RETRY),  # not implemented
    (502, 10002, RETRY),
    (503, 10100, RETRY),  # authentication upstream, transient
    (504, 10002, RETRY),
    (520, 10002, RETRY),  # Cloudflare's own 52x range
]


class TestClassification:
    @pytest.mark.parametrize(
        ("status", "code", "outcome"), CLASSIFICATION, ids=[f"{s}-{c}" for s, c, _ in CLASSIFICATION]
    )
    def test_status_and_code(self, cloudflare_configured, status, code, outcome):
        with patch.object(_mail.httpx, "post", return_value=_error(status, code, "email.sending.error.x")):
            delivery = deliver(_message())
        assert delivery.outcome == outcome
        assert delivery.detail == f"HTTP {status}: {code} email.sending.error.x"
        assert delivery.message_id is None
        assert delivery.account_fault is (status in (401, 403, 404))

    def test_an_accepted_message_is_sent(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=_answer()):
            delivery = deliver(_message())
        assert delivery == Delivery(SENT, "queued", message_id="<m@retina.fm>")

    @pytest.mark.parametrize(
        ("status", "outcome"),
        [(502, RETRY), (403, RETRY), (422, REJECTED), (200, RETRY)],
        ids=["502", "403", "422", "200"],
    )
    def test_a_body_that_is_not_json_does_not_raise_or_leak(self, cloudflare_configured, caplog, status, outcome):
        page = "<html>Bad gateway, request was: Click this link</html>"
        with patch.object(_mail.httpx, "post", return_value=_answer(status, text=page)):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        assert delivery.outcome == outcome
        assert delivery.detail.startswith(f"HTTP {status}: ")
        assert "Click this link" not in delivery.detail
        assert "Click this link" not in caplog.text

    def test_a_2xx_that_does_not_claim_success_is_retried(self, cloudflare_configured):
        """Whether it went is unknown, and a second copy costs less than none."""
        answer = _answer(200, {"success": False, "errors": [], "result": None})
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()).outcome == RETRY

    def test_an_error_response_is_logged_with_cloudflares_reason(self, cloudflare_configured, caplog):
        answer = _error(400, 10202, "email.sending.error.email.invalid")
        with patch.object(_mail.httpx, "post", return_value=answer):
            with caplog.at_level(logging.ERROR):
                assert deliver(_message()).outcome == REJECTED
        assert "owner@example.com" in caplog.text
        assert "HTTP 400" in caplog.text
        assert "10202 email.sending.error.email.invalid" in caplog.text

    def test_every_error_is_named(self, cloudflare_configured):
        answer = _answer(
            400,
            {"success": False, "errors": [{"code": 10001, "message": "a"}, {"code": 10202, "message": "b"}]},
        )
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()).detail == "HTTP 400: 10001 a; 10202 b"

    def test_the_detail_fits_the_outbox_row(self, cloudflare_configured):
        errors = [{"code": 10001, "message": "m" * 200} for _ in range(10)]
        with patch.object(_mail.httpx, "post", return_value=_answer(400, {"success": False, "errors": errors})):
            delivery = deliver(_message())
        assert delivery.outcome == REJECTED
        assert len(delivery.detail) == 512

    @pytest.mark.parametrize(
        "errors",
        [None, "oops", [None, 3, "x"], [{}, {"code": None, "message": None}, {"code": None, "message": ""}]],
        ids=["null", "string", "junk-items", "empty-items"],
    )
    def test_a_malformed_error_list_still_classifies(self, cloudflare_configured, errors):
        with patch.object(_mail.httpx, "post", return_value=_answer(400, {"success": False, "errors": errors})):
            delivery = deliver(_message())
        assert delivery == Delivery(REJECTED, "HTTP 400: Bad Request")

    def test_a_missing_code_or_message_is_left_out_rather_than_printed_as_none(self, cloudflare_configured):
        """The schema lets `code` be null, and "None" in the outbox row reads
        as if something on this side had broken."""
        answer = _refusal(
            400,
            {"code": None, "message": "a"},
            {"message": "b"},
            {"code": 10001, "message": None},
            {"code": 10202},
            {"code": 10200, "message": ""},
            {"code": None, "message": None},
        )
        with patch.object(_mail.httpx, "post", return_value=answer):
            delivery = deliver(_message())
        assert delivery == Delivery(REJECTED, "HTTP 400: a; b; 10001; 10202; 10200")
        assert "None" not in delivery.detail


class TestSuppressedRecipient:
    """With "Drop suppressed recipients" off, the default, a suppressed
    recipient fails the whole request with a 400 instead of coming back in a
    200's result.suppressed_recipients. Either way the address cannot receive,
    so the outbox gives up on the address and not only on this message."""

    @pytest.mark.parametrize(
        ("error", "detail"),
        [
            ({"code": None, "message": SUPPRESSED}, f"HTTP 400: {SUPPRESSED}"),
            ({"message": SUPPRESSED}, f"HTTP 400: {SUPPRESSED}"),
            ({"code": 10999, "message": SUPPRESSED}, f"HTTP 400: 10999 {SUPPRESSED}"),
        ],
        ids=["code-null", "code-absent", "code-number"],
    )
    def test_is_undeliverable(self, cloudflare_configured, caplog, error, detail):
        with patch.object(_mail.httpx, "post", return_value=_refusal(400, error)):
            with caplog.at_level(logging.WARNING):
                delivery = deliver(_message())
        assert delivery == Delivery(UNDELIVERABLE, detail)
        # Logged as an address that cannot receive, not as a refused message.
        assert "owner@example.com" in caplog.text
        assert "is undeliverable" in caplog.text
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    @pytest.mark.parametrize(
        ("errors", "detail"),
        [
            (
                [
                    {"code": None, "message": SUPPRESSED},
                    {"code": 10001, "message": "email.sending.error.invalid_request_schema"},
                ],
                f"HTTP 400: {SUPPRESSED}; 10001 email.sending.error.invalid_request_schema",
            ),
            (
                [{"code": 10200, "message": "email.sending.error.email.too_big"}, {"message": SUPPRESSED}],
                f"HTTP 400: 10200 email.sending.error.email.too_big; {SUPPRESSED}",
            ),
        ],
        ids=["first", "last"],
    )
    def test_beside_other_errors_is_still_undeliverable(self, cloudflare_configured, errors, detail):
        """However the rest of the request were fixed, the address would not take it."""
        with patch.object(_mail.httpx, "post", return_value=_refusal(400, *errors)):
            assert deliver(_message()) == Delivery(UNDELIVERABLE, detail)

    def test_the_detail_is_cloudflares_reason_and_nothing_of_the_request(self, cloudflare_configured, caplog):
        message = _message(
            subject="Sign in to RETINA", text=f"Click:\n{LONG_LINK}\n", html=f'<a href="{LONG_LINK}">x</a>'
        )
        with patch.object(_mail.httpx, "post", return_value=_refusal(400, {"code": None, "message": SUPPRESSED})):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(message)
        assert delivery.detail == f"HTTP 400: {SUPPRESSED}"
        for part in ("owner@example.com", "Sign in to RETINA", "Click", LONG_LINK):
            assert part not in delivery.detail
        assert LONG_LINK not in caplog.text

    @pytest.mark.parametrize(
        "reason",
        [
            "email.sending.error.invalid_request_schema",
            "email.sending.error.email.invalid",
            "email.sending.error.email.no_content_length",
            "email.sending.error.email.too_big",
        ],
    )
    def test_every_other_400_is_still_rejected(self, cloudflare_configured, reason):
        """The rest of the 400 schema's messages are about the message."""
        with patch.object(_mail.httpx, "post", return_value=_refusal(400, {"code": None, "message": reason})):
            assert deliver(_message()) == Delivery(REJECTED, f"HTTP 400: {reason}")

    @pytest.mark.parametrize(("status", "outcome"), [(403, RETRY), (422, REJECTED), (500, RETRY)])
    def test_only_a_400_says_so(self, cloudflare_configured, status, outcome):
        """The API documents it on a 400 alone. An undeliverable address has
        the links that went to it killed, so a status it was never documented
        on keeps that status's own outcome."""
        with patch.object(_mail.httpx, "post", return_value=_refusal(status, {"code": None, "message": SUPPRESSED})):
            assert deliver(_message()).outcome == outcome

    def test_the_same_recipient_in_a_200_is_still_undeliverable(self, cloudflare_configured):
        """What a domain that drops suppressed recipients answers instead."""
        result = {
            "delivered": [],
            "queued": [],
            "permanent_bounces": [],
            "suppressed_recipients": ["owner@example.com"],
        }
        answer = _answer(payload={"success": True, "errors": [], "result": result})
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()) == Delivery(UNDELIVERABLE, "suppressed recipient")


class TestSendingNotReady:
    """A 409 says Email Sending is still being set up for the account and that
    nothing was sent. That is no verdict on the message or on the address."""

    @pytest.mark.parametrize(
        ("error", "detail"),
        [
            ({"code": None, "message": NOT_READY}, f"HTTP 409: {NOT_READY}"),
            ({"message": NOT_READY}, f"HTTP 409: {NOT_READY}"),
            ({"code": 10999, "message": NOT_READY}, f"HTTP 409: 10999 {NOT_READY}"),
        ],
        ids=["code-null", "code-absent", "code-number"],
    )
    def test_is_retried(self, cloudflare_configured, caplog, error, detail):
        with patch.object(_mail.httpx, "post", return_value=_refusal(409, error)):
            with caplog.at_level(logging.WARNING):
                delivery = deliver(_message())
        assert delivery == Delivery(RETRY, detail)
        assert "worth retrying" in caplog.text
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    @pytest.mark.parametrize(
        ("header", "expected"),
        [("30", 30.0), ("0", 0.0), ("86400", 86400.0), ("86401", None), ("soon", None), ("-5", None)],
    )
    def test_waits_as_long_as_a_sane_retry_after_asks(self, cloudflare_configured, header, expected):
        answer = _refusal(409, {"code": None, "message": NOT_READY}, headers={"Retry-After": header})
        with patch.object(_mail.httpx, "post", return_value=answer):
            delivery = deliver(_message())
        assert delivery.outcome == RETRY
        assert delivery.retry_after_s == expected

    @pytest.mark.parametrize(
        "answer",
        [
            _refusal(409, {"code": 10000, "message": "email.sending.error.x"}),
            _refusal(409),
            _answer(409, text="<html>Conflict</html>"),
        ],
        ids=["other-message", "no-errors", "not-json"],
    )
    def test_any_409_is_retried(self, cloudflare_configured, answer):
        """A conflict is about the state of the account, whatever it says."""
        with patch.object(_mail.httpx, "post", return_value=answer):
            delivery = deliver(_message())
        assert delivery.outcome == RETRY
        assert delivery.detail.startswith("HTTP 409: ")


#: The refusals Cloudflare documents as faults of the token, the account or the
#: sending setup: its REST error table, plus the 403 its OpenAPI schema adds for
#: a sending domain that is not set up, which has no published code.
ACCOUNT_FAULTS = [
    (401, {"code": 10101, "message": "email.sending.error.authentication.unauthorized"}),
    (401, {"code": 10103, "message": "email.sending.error.authentication.bad_token_type"}),
    (403, {"code": 10102, "message": "email.sending.error.authentication.forbidden"}),
    (403, {"code": 10105, "message": "email.sending.error.authentication.not_entitled"}),
    (403, {"code": 10203, "message": "email.sending.error.email.sending_disabled"}),
    (403, {"code": None, "message": "email.sending.error.email.sender_not_configured"}),
    (404, {"code": 10000, "message": "email.sending.error.not_found"}),
]


class TestAccountFault:
    """A refusal of the token, the account or the sending domain says nothing
    against the message, and every mail gets the same answer until somebody
    fixes the setting. So the mail is held for a retry rather than dropped, and
    the attempt logs one error that says what to fix and names no recipient."""

    @pytest.mark.parametrize(
        ("status", "error"), ACCOUNT_FAULTS, ids=[f"{s}-{e['message'].rsplit('.', 1)[1]}" for s, e in ACCOUNT_FAULTS]
    )
    def test_is_retried_and_logged_once_as_an_error(self, cloudflare_configured, caplog, status, error):
        reason = _mail._reason(error)
        with patch.object(_mail.httpx, "post", return_value=_refusal(status, error)):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        assert (delivery.outcome, delivery.detail) == (RETRY, f"HTTP {status}: {reason}")
        assert delivery == Delivery(RETRY, f"HTTP {status}: {reason}", account_fault=True)
        [record] = caplog.records
        assert record.levelno == logging.ERROR
        logged = record.getMessage()
        assert f"HTTP {status}: {reason}" in logged
        assert "CLOUDFLARE_EMAIL_TOKEN" in logged
        assert "owner@example.com" not in logged
        assert "token-value-not-in-logs" not in logged

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (400, {"code": 10202, "message": "email.sending.error.email.invalid"}),
            (422, {"code": 10001, "message": "email.sending.error.invalid_request_schema"}),
        ],
        ids=["400", "422"],
    )
    def test_a_refusal_of_the_message_is_still_rejected(self, cloudflare_configured, caplog, status, error):
        with patch.object(_mail.httpx, "post", return_value=_refusal(status, error)):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        assert delivery == Delivery(REJECTED, f"HTTP {status}: {_mail._reason(error)}")
        [record] = caplog.records
        assert record.levelno == logging.ERROR
        assert "Mail to 'owner@example.com' refused" in record.getMessage()

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (409, {"code": None, "message": NOT_READY}),
            (429, {"code": 10004, "message": "email.sending.error.throttled"}),
            (503, {"code": 10100, "message": "email.sending.error.authentication.upstream"}),
        ],
        ids=["409", "429", "503"],
    )
    def test_a_wait_the_account_will_get_past_on_its_own_is_only_a_warning(
        self, cloudflare_configured, caplog, status, error
    ):
        """Setup still in progress, a throttle and a blip in Cloudflare's own
        authentication pass without anyone doing anything."""
        with patch.object(_mail.httpx, "post", return_value=_refusal(status, error)):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert "worth retrying" in record.getMessage()
        assert (delivery.outcome, delivery.account_fault) == (RETRY, False)

    def test_a_reason_that_quotes_the_token_is_withheld(self, cloudflare_configured, caplog):
        """Cloudflare's reason is logged as an error for people to read, and
        kept in the outbox row, so it must not carry the token whatever it says."""
        error = {"code": 10101, "message": "invalid token token-value-not-in-logs"}
        with patch.object(_mail.httpx, "post", return_value=_refusal(401, error)):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        assert "token-value-not-in-logs" not in caplog.text
        assert delivery == Delivery(RETRY, "HTTP 401: reason withheld, it quotes the API token", account_fault=True)


class TestRetryAfter:
    @pytest.mark.parametrize(
        ("status", "header", "expected"),
        [
            (429, "30", 30.0),
            (503, "120", 120.0),
            (429, "0", 0.0),
            (429, " 45 ", 45.0),
            (429, "86400", 86400.0),
            (429, None, None),
            (429, "", None),
            (429, "soon", None),
            (429, "-5", None),
            (429, "1.5", None),
            (429, "nan", None),
            (429, "١٢".encode(), None),  # digits, but not the ASCII ones delay-seconds is made of
            (429, "86401", None),
            (429, "99999999999999999999999", None),
            (409, "30", 30.0),
            (409, None, None),
            (409, "86401", None),
            (401, "30", 30.0),
            (403, "60", 60.0),
            (403, None, None),
            (403, "86401", None),
            (404, "15", 15.0),
            (500, "30", None),
            (502, "30", None),
        ],
        ids=lambda value: repr(value),
    )
    def test_seconds(self, cloudflare_configured, status, header, expected):
        headers = {"Retry-After": header} if header is not None else None
        answer = _answer(status, {"success": False, "errors": [{"code": 1, "message": "x"}]}, headers=headers)
        with patch.object(_mail.httpx, "post", return_value=answer):
            delivery = deliver(_message())
        assert delivery.outcome == RETRY
        assert delivery.retry_after_s == expected

    @pytest.mark.parametrize("status", [403, 409, 429, 503])
    def test_an_http_date_is_read_as_the_time_left(self, cloudflare_configured, status):
        when = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
        answer = _answer(status, {"success": False, "errors": []}, headers={"Retry-After": when})
        with patch.object(_mail.httpx, "post", return_value=answer):
            delivery = deliver(_message())
        assert delivery.retry_after_s is not None
        assert 110 <= delivery.retry_after_s <= 120

    def test_an_http_date_already_past_means_now(self, cloudflare_configured):
        answer = _answer(
            503, {"success": False, "errors": []}, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
        )
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()).retry_after_s == 0.0

    def test_an_http_date_days_away_is_not_taken_at_its_word(self, cloudflare_configured):
        when = format_datetime(datetime.now(UTC) + timedelta(days=2), usegmt=True)
        answer = _answer(429, {"success": False, "errors": []}, headers={"Retry-After": when})
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()).retry_after_s is None

    def test_a_refusal_carries_no_wait(self, cloudflare_configured):
        answer = _answer(400, {"success": False, "errors": []}, headers={"Retry-After": "30"})
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()).retry_after_s is None


class TestAccepted:
    @staticmethod
    def _accepted(**result) -> httpx.Response:
        return _answer(payload={"success": True, "errors": [], "result": result})

    def test_message_id_is_passed_through(self, cloudflare_configured):
        with patch.object(_mail.httpx, "post", return_value=self._accepted(message_id="<abc@retina.fm>", queued=["o"])):
            assert deliver(_message()).message_id == "<abc@retina.fm>"

    @pytest.mark.parametrize("message_id", [None, "", 42, ["<a@b>"]], ids=["null", "empty", "number", "list"])
    def test_a_message_id_that_is_not_one_is_dropped(self, cloudflare_configured, message_id):
        with patch.object(_mail.httpx, "post", return_value=self._accepted(message_id=message_id, queued=["o"])):
            delivery = deliver(_message())
        assert delivery.outcome == SENT
        assert delivery.message_id is None

    @pytest.mark.parametrize(
        ("result", "detail"),
        [
            ({"delivered": ["owner@example.com"], "queued": []}, "delivered"),
            ({"delivered": [], "queued": ["owner@example.com"]}, "queued"),
            ({}, "accepted"),
        ],
        ids=["delivered", "queued", "neither"],
    )
    def test_says_how_it_was_taken(self, cloudflare_configured, result, detail):
        """Accepted without saying how is still accepted: a retry would mail
        the same link twice on the strength of a field going missing."""
        with patch.object(_mail.httpx, "post", return_value=self._accepted(**result)):
            delivery = deliver(_message())
        assert (delivery.outcome, delivery.detail) == (SENT, detail)

    @pytest.mark.parametrize("result", [None, "x", []], ids=["null", "string", "list"])
    def test_a_result_that_is_not_an_object_is_still_accepted(self, cloudflare_configured, result):
        answer = _answer(payload={"success": True, "errors": [], "result": result})
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()) == Delivery(SENT, "accepted")

    @pytest.mark.parametrize(
        ("field", "detail"),
        [("permanent_bounces", "permanent bounce"), ("suppressed_recipients", "suppressed recipient")],
    )
    def test_an_undeliverable_recipient_is_flagged(self, cloudflare_configured, caplog, field, detail):
        """Cloudflare answers 200 for these, so success is not the status code.
        The recipient is also listed nowhere else it could be read from."""
        result = {"delivered": [], "queued": [], "permanent_bounces": [], "suppressed_recipients": []}
        result[field] = ["owner@example.com"]
        with patch.object(_mail.httpx, "post", return_value=self._accepted(**result)):
            with caplog.at_level(logging.WARNING):
                delivery = deliver(_message())
        assert delivery == Delivery(UNDELIVERABLE, detail)
        assert "owner@example.com" in caplog.text
        assert detail in caplog.text

    def test_a_bounce_outweighs_a_queue(self, cloudflare_configured):
        answer = self._accepted(queued=["owner@example.com"], permanent_bounces=["owner@example.com"])
        with patch.object(_mail.httpx, "post", return_value=answer):
            assert deliver(_message()).outcome == UNDELIVERABLE


# ── Never raising ─────────────────────────────────────────────────────────────


class TestNeverRaises:
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("unreachable"),
            httpx.ReadTimeout("timed out"),
            httpx.WriteTimeout("timed out"),
            httpx.RemoteProtocolError("server disconnected"),
            httpx.InvalidURL("bad account id"),
            UnicodeEncodeError("ascii", "tök", 1, 2, "ordinal not in range(128)"),
            RuntimeError("anything else"),
        ],
        ids=["unreachable", "read-timeout", "write-timeout", "protocol", "invalid-url", "non-ascii-token", "other"],
    )
    def test_a_request_that_gets_no_answer_is_retried(self, cloudflare_configured, caplog, error):
        """Nothing came back, so nothing says the message is at fault. A
        failure must not reach the caller as an exception either: the outbox
        would be left with a message in flight and no reason why."""
        with patch.object(_mail.httpx, "post", side_effect=error):
            with caplog.at_level(logging.WARNING):
                delivery = deliver(_message())
        assert delivery.outcome == RETRY
        assert delivery.detail.startswith(type(error).__name__)
        assert "owner@example.com" in caplog.text

    def test_a_failure_past_the_request_is_retried_and_logged(self, cloudflare_configured, caplog):
        with patch.object(_mail.httpx, "post", return_value=_answer()):
            with patch.object(_mail, "_classify", side_effect=KeyError("surprise")):
                with caplog.at_level(logging.ERROR):
                    delivery = deliver(_message())
        assert delivery == Delivery(RETRY, "unexpected KeyError")
        assert "owner@example.com" in caplog.text

    @pytest.mark.parametrize("field", ["to", "subject"])
    @pytest.mark.parametrize("linebreak", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
    def test_a_linebreak_in_a_header_is_refused_without_raising(self, cloudflare_configured, caplog, field, linebreak):
        """EmailMessage's default policy rejects a header value carrying a
        linefeed, which is what stops "a@b.com\\nBcc: attacker@evil.com" adding
        a recipient. It rejects by raising, so this must be caught: a hostile
        address answering differently from an unknown one is the enumeration
        oracle the sign-in route exists to avoid."""
        hostile = f"someone@example.com{linebreak}Bcc: attacker@evil.com"
        to = hostile if field == "to" else "owner@example.com"
        subject = hostile if field == "subject" else "Sign in"
        with patch.object(_mail.httpx, "post") as post:
            with caplog.at_level(logging.ERROR):
                assert deliver(_message(to=to, subject=subject)).outcome == REJECTED
        post.assert_not_called()
        # Nor does it forge a log line of its own.
        assert all("\n" not in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize(
        "to",
        ["", "owner", "owner@localhost", "two words@example.com", "a@b@example.com", " owner@example.com"],
        ids=["empty", "no-at", "bare-host", "space", "two-ats", "padded"],
    )
    def test_an_invalid_address_is_refused(self, cloudflare_configured, to):
        with patch.object(_mail.httpx, "post") as post:
            delivery = deliver(_message(to=to))
        assert delivery == Delivery(REJECTED, "not an email address")
        post.assert_not_called()

    @pytest.mark.parametrize("field", ["text", "html"])
    def test_a_body_that_cannot_be_encoded_is_refused(self, cloudflare_configured, field):
        """A lone surrogate has no UTF-8 form, so the message can never go."""
        broken = {field: "half a character: \ud800"}
        with patch.object(_mail.httpx, "post") as post:
            delivery = deliver(_message(**broken))
        assert delivery.outcome == REJECTED
        assert "UnicodeEncodeError" in delivery.detail
        post.assert_not_called()


class TestTokenNeverLogged:
    @pytest.mark.parametrize(
        "failure",
        [
            {"return_value": _error(403, 10102, "nope")},
            {"return_value": _error(422, 10001, "nope")},
            {"return_value": _answer(429, text="slow down")},
            {"side_effect": httpx.ConnectError("nope")},
        ],
        ids=["account-fault", "refused", "throttled", "network"],
    )
    def test_not_on_failure(self, cloudflare_configured, caplog, failure):
        with patch.object(_mail.httpx, "post", **failure):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        assert "token-value-not-in-logs" not in caplog.text
        assert "token-value-not-in-logs" not in delivery.detail

    @pytest.mark.parametrize("control", ["\n", "\r", "\x0c"], ids=["lf", "cr", "ff"])
    def test_not_when_the_transport_quotes_it(self, cloudflare_configured, monkeypatch, caplog, control):
        """h11 refuses a header value it cannot send by quoting the value, so a
        token with a stray control character in it would otherwise reach the
        log and the outbox row whole."""
        token = f"token-value{control}not-in-logs"
        monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", token)
        quoted = httpx.LocalProtocolError(f"Illegal header value {('Bearer ' + token).encode()!r}")
        with patch.object(_mail.httpx, "post", side_effect=quoted):
            with caplog.at_level(logging.DEBUG):
                delivery = deliver(_message())
        assert delivery.outcome == RETRY
        assert delivery.detail == "LocalProtocolError: message withheld, it quotes the API token"
        for form in (token, repr(token)[1:-1], "not-in-logs"):
            assert form not in caplog.text
            assert form not in delivery.detail


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

    def test_log_transport_is_announced_loudly(self, log_configured, caplog):
        """Mail going to a log file rather than a mailbox is worth a WARNING,
        so it shows up in a deploy's own logs and not only in a terminal."""
        with caplog.at_level(logging.INFO):
            _mail.log_destination()
        assert any(r.levelno == logging.WARNING for r in caplog.records)
