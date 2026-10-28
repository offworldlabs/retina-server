"""Outbound transactional mail over Cloudflare Email Service.

The Email Sending REST API rather than authenticated SMTP or the Workers
binding. DigitalOcean drops outbound traffic to ports 25, 465 and 587 on every
droplet, so SMTP to smtp.mx.cloudflare.net times out from where this runs, and
the backend is not a Worker. All three enter the same pipeline, with the same
DKIM signing and the same delivery logs.

Settings are read from the environment on each call rather than once at import.
main.py calls load_dotenv() after its service imports, so an import-time read
sees an empty environment on any start that does not already carry the
variables (a bare `python main.py`), which would leave mail silently disabled.

Nothing here refuses to boot. A misconfigured mailer costs the login flow, and
the process it would refuse also carries the radar pipeline; callers ask
is_configured() and fail their own feature closed instead.

deliver() makes one attempt and reports it rather than raising. Each outcome
says what to do next, not what went wrong: try again later, give up on the
message, or give up on the address. Retrying, pacing and bookkeeping belong to
the outbox that calls it.
"""

import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

import httpx

from services.email_address import is_address

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    """One mail to one recipient, as plain text and as HTML; the client picks."""

    to: str
    subject: str
    text: str
    html: str


#: The transport took it. Cloudflare: delivered or queued; log: written to the log.
SENT = "sent"
#: Try again later: no answer, a timeout (408), a 409 while Email Sending is
#: still being set up (sending_not_ready), 429, any 5xx (503/10100 included),
#: mail not configured right now, or a 401, 403 or 404, which fault the token,
#: the account or the sending domain rather than the message and are logged as
#: errors for somebody to fix.
RETRY = "retry"
#: Give up on the message: it will never go as it stands, though the address
#: may still take another. A 400 about the request itself (its schema, its
#: content, its size), any 4xx not named above, a header it cannot carry, an
#: invalid address.
REJECTED = "rejected"
#: Give up on the address, not only the message: the recipient is in a 200's
#: result.permanent_bounces or result.suppressed_recipients, or a 400 says
#: recipient_suppressed, which is how the API refuses a suppressed recipient
#: unless the domain drops them.
UNDELIVERABLE = "undeliverable"


@dataclass(frozen=True)
class Delivery:
    outcome: str  # one of the four above
    detail: str = ""  # short reason for logs and the outbox row; never the body or a token; at most 512 chars
    retry_after_s: float | None = None  # Retry-After, in seconds, on 401/403/404/409/429/503, when present and sane
    message_id: str | None = None  # result.message_id on SENT, when present
    account_fault: bool = False  # on RETRY: refused over the token, the account or the sending domain, not the message


_SEND_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/email/sending/send"

#: The error message of a 400 for a suppressed recipient. Cloudflare publishes
#: no numeric code for it and its schema lets `code` be null, so the message is
#: what it is known by.
_RECIPIENT_SUPPRESSED = "email.sending.error.email.recipient_suppressed"

#: Refusals Cloudflare documents as faults of the account rather than of the
#: message. 401: a missing or invalid token (10101), or the wrong type of token
#: (10103). 403: a token that may not send (10102), an account not entitled to
#: Email Sending (10105), sending disabled for the zone or account (10203), or a
#: sending domain not set up for it (sender_not_configured). 404: not found
#: (10000), and the account in the URL is the only thing the send endpoint
#: looks up. Every mail gets the same answer until somebody fixes the setting,
#: which can happen while a link is still good.
_ACCOUNT_FAULTS = frozenset({401, 403, 404})

#: The outbox runs deliver() on a worker thread, so a request that never
#: returned would hold that thread, and the message with it, for good.
_TIMEOUT_S = 15.0

#: The longest line a 7bit or 8bit body may carry, in octets and not counting
#: the line break (RFC 5322, 2.1.1).
_MAX_LINE_OCTETS = 998

#: Delivery.detail ends up in an outbox column as well as the log.
_MAX_DETAIL = 512

#: Longest Retry-After taken at its word. A throttle asks for seconds or
#: minutes; a day or more is a broken header, and the outbox's own backoff is
#: the better guide.
_MAX_RETRY_AFTER_S = 86_400.0

#: `log` writes what would have been sent to the application log, for a laptop
#: with no Cloudflare token. It never resolves in production: a magic link in a
#: log file is a credential in a log file.
_TRANSPORTS = ("cloudflare", "log")


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


def transport() -> str:
    """The transport to use, or "" when mail is not usable as configured.

    Every reason for "" is checked here rather than at each call site, so
    is_configured() and deliver() cannot disagree about what is set up.
    """
    chosen = _setting("MAIL_TRANSPORT").lower()
    if not chosen:
        return ""
    if chosen not in _TRANSPORTS:
        logger.warning(
            "MAIL_TRANSPORT=%r is not one of %s; no mail will be sent.",
            chosen,
            ", ".join(_TRANSPORTS),
        )
        return ""
    if chosen == "log" and _setting("RETINA_ENV").lower() == "production":
        logger.warning(
            "MAIL_TRANSPORT=log is refused in production: it would write sign-in links to the "
            "application log instead of delivering them. No mail will be sent."
        )
        return ""
    if not _setting("MAIL_FROM"):
        logger.warning("MAIL_FROM is not set; no mail will be sent.")
        return ""
    if chosen == "cloudflare":
        for name in ("CLOUDFLARE_EMAIL_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
            if not _setting(name):
                logger.warning("%s is not set; no mail will be sent.", name)
                return ""
    return chosen


def link_to(path: str) -> str | None:
    """The URL a mail carries for `path` on HOST_APP, or None when HOST_APP is unset.

    Built from configuration, never from a request. `request.base_url` derives
    from the Host header, and an attacker who could set it would ask for a link
    to somebody else's address and have the mail carry a URL pointing at their
    own server: the recipient clicks, and the token is handed over. Callers
    refuse or drop the send when this is None rather than fall back on a
    request's host, which would restore exactly that.
    """
    host = os.getenv("HOST_APP", "").strip()
    if not host:
        return None
    scheme = "https" if os.getenv("FORCE_HTTPS", "true").lower() == "true" else "http"
    return f"{scheme}://{host}{path}"


def is_configured() -> bool:
    return transport() != ""


def log_destination() -> None:
    """Say once, at boot, where mail is going. Silent when unconfigured."""
    chosen = transport()
    if not chosen:
        return
    sender = _setting("MAIL_FROM")
    if chosen == "log":
        logger.warning(
            "MAIL_TRANSPORT=log: mail as %s is written to this log and delivered to nobody.",
            sender,
        )
        return
    logger.info("Mail: %s Email Sending API as %s", chosen, sender)


def _encoding(body: str) -> str | None:
    """The transfer encoding for one part, or None to leave it to the library.

    7bit rather than the default: a sign-in URL runs past the 78 columns that
    picks quoted-printable, and QP breaks it across two lines with a soft `=`.
    A compliant client rejoins them, but a plain-text view or a copy-paste hands
    the reader half a token, and the link is the whole credential. A body that
    is not ASCII gets 8bit on the same grounds, where the default would pick QP
    or base64. Both are only valid while every line fits in 998 octets; past
    that the library's choice is the one the RFC allows.
    """
    octets = body.encode("utf-8")
    if any(len(line) > _MAX_LINE_OCTETS for line in octets.splitlines()):
        return None
    return "7bit" if octets.isascii() else "8bit"


def _build(message: Message) -> EmailMessage:
    """The message as MIME: the text, then the HTML as its alternative.

    multipart/alternative lists its parts from least to most preferred, so a
    client that can show HTML shows the HTML and one that cannot shows the text.
    """
    mime = EmailMessage()
    mime["From"] = _setting("MAIL_FROM")
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime.set_content(message.text, cte=_encoding(message.text))
    mime.add_alternative(message.html, subtype="html", cte=_encoding(message.html))
    return mime


def _retry_after(response: httpx.Response) -> float | None:
    """The wait a Retry-After header asks for, in seconds, or None when there
    is none worth obeying.

    Cloudflare's REST APIs send delay-seconds. An HTTP-date is the header's
    other form (RFC 9110, 10.2.3) and is read as the time left until it.
    """
    value = response.headers.get("retry-after", "").strip()
    if value.isascii() and value.isdigit():
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            # An HTTP-date is always GMT; "-0000" is how email.utils says so.
            when = when.replace(tzinfo=UTC)
        seconds = max((when - datetime.now(UTC)).total_seconds(), 0.0)
    return seconds if seconds <= _MAX_RETRY_AFTER_S else None


def _accepted(result: object) -> Delivery:
    """What a 2xx with `success: true` means for the one recipient.

    A suppressed recipient comes back here, in result.suppressed_recipients,
    only when the sending domain has "Drop suppressed recipients" on. With it
    off, which is the default, the same recipient is a 400 instead, and
    _classify makes that UNDELIVERABLE too.
    """
    if not isinstance(result, dict):
        result = {}
    # Both come back with a 200 and success: true, so neither shows in the status.
    if result.get("permanent_bounces"):
        return Delivery(UNDELIVERABLE, "permanent bounce")
    if result.get("suppressed_recipients"):
        return Delivery(UNDELIVERABLE, "suppressed recipient")
    message_id = result.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        message_id = None
    # Accepted without saying how is still accepted: trying again would mail
    # the same link twice on the strength of a field going missing.
    state = "delivered" if result.get("delivered") else "queued" if result.get("queued") else "accepted"
    return Delivery(SENT, state, message_id=message_id)


def _reason(error: dict) -> str:
    """One entry of an error list as "<code> <message>", without a part that is missing.

    The schema lets `code` be null, and "None ..." in a log line reads as if
    something here had broken.
    """
    return " ".join(str(part) for part in (error.get("code"), error.get("message")) if part is not None and part != "")


def _classify(response: httpx.Response) -> Delivery:
    """The outcome of one answer from the send endpoint.

    A 4xx is a verdict on the message, and REJECTED, except for those below.
    A 400 that says recipient_suppressed is how the API refuses a suppressed
    recipient while the domain keeps "Drop suppressed recipients" off, the
    default. The address cannot receive, so it is UNDELIVERABLE, as it is when
    a 200 lists it in result.suppressed_recipients. A 409 says Email Sending is
    still being set up (sending_not_ready) and nothing was sent, so it is RETRY.
    So is a 401, 403 or 404, which says the token, the account or the sending
    domain is wrong: somebody can fix that while the link is still good, and it
    is flagged as an account fault so that deliver() logs it as an error. Each
    waits as long as a Retry-After header asks, if it has one.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if response.is_success and payload.get("success") is True:
        return _accepted(payload.get("result"))

    errors = payload.get("errors")
    errors = [error for error in errors if isinstance(error, dict)] if isinstance(errors, list) else []
    reasons = "; ".join(reason for reason in map(_reason, errors) if reason)
    # Cloudflare's codes and messages, never the body: an error page from
    # something in between could echo the request, and the request holds a link.
    detail = f"HTTP {response.status_code}: {reasons or response.reason_phrase}"
    if _quotes_token(detail):
        # The detail goes to the log and the outbox row, and an account fault
        # logs it as an error for people to read.
        detail = f"HTTP {response.status_code}: reason withheld, it quotes the API token"
    status = response.status_code
    if status == 429 or status >= 500:
        # 503 includes 10100, the authentication service being briefly away.
        retry_after = _retry_after(response) if status in (429, 503) else None
        return Delivery(RETRY, detail, retry_after_s=retry_after)
    if status == 408:
        # The server's own timeout, not a verdict on the message.
        return Delivery(RETRY, detail)
    if status == 409:
        # sending_not_ready is the only 409 the endpoint documents. A conflict
        # is about the state of the account, not the message, whatever it says.
        return Delivery(RETRY, detail, retry_after_s=_retry_after(response))
    if status in _ACCOUNT_FAULTS:
        # By status rather than by code: the schema lets `code` be null, and
        # whatever the body says, nothing in the message earns one of these.
        return Delivery(RETRY, detail, retry_after_s=_retry_after(response), account_fault=True)
    if status == 400 and any(error.get("message") == _RECIPIENT_SUPPRESSED for error in errors):
        # Beside other errors too: a message to an address that cannot
        # receive will not go however the rest of it is fixed.
        return Delivery(UNDELIVERABLE, detail)
    if 400 <= status < 500:
        return Delivery(REJECTED, detail)
    # A 2xx that does not claim success, or a status nothing here asked for:
    # whether it went is unknown, and a second copy costs less than none.
    return Delivery(RETRY, detail)


def _quotes_token(text: str) -> bool:
    """Whether `text` carries the API token, as typed or as Python quotes it.

    h11 refuses a header value it cannot send by quoting the value, so a token
    with a stray control character in it (a quoted multi-line value in .env, say)
    would reach the log and the outbox row as "Illegal header value b'Bearer ...'".
    """
    token = _setting("CLOUDFLARE_EMAIL_TOKEN")
    if not token:
        return False
    forms = (token, repr(token)[1:-1], repr(token.encode("utf-8", "backslashreplace"))[2:-1])
    return any(form in text for form in forms)


def _post(message: Message) -> Delivery:
    """Hand one message to Cloudflare."""
    name, address = parseaddr(_setting("MAIL_FROM"))
    sender = {"address": address, "name": name} if name else {"address": address}
    try:
        response = httpx.post(
            _SEND_URL.format(account=_setting("CLOUDFLARE_ACCOUNT_ID")),
            headers={"Authorization": f"Bearer {_setting('CLOUDFLARE_EMAIL_TOKEN')}"},
            json={
                "from": sender,
                "to": [message.to],
                "subject": message.subject,
                "text": message.text,
                "html": message.html,
            },
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        # No answer, so nothing says the message is at fault: a refused
        # connection, a timeout, or a request the settings could not form.
        reason = str(exc)
        if _quotes_token(reason):
            reason = "message withheld, it quotes the API token"
        return Delivery(RETRY, f"{type(exc).__name__}: {reason}")
    return _classify(response)


def _attempt(message: Message) -> Delivery:
    # The message is judged before the configuration: one that can never go
    # is refused now, not held for a retry that would refuse it later.
    if not is_address(message.to):
        return Delivery(REJECTED, "not an email address")
    try:
        # Built and written out for both transports. The API is handed the
        # parts as JSON and composes its own MIME, but this is what refuses a
        # header the mail cannot carry: EmailMessage's default policy rejects a
        # value with a linefeed, which is how an address like
        # "someone@example.com\nBcc: ..." is stopped before the API is handed
        # it. It rejects by raising, and the same message raises every time.
        raw = _build(message).as_bytes()
    except Exception as exc:
        return Delivery(REJECTED, f"cannot be written as a mail: {type(exc).__name__}: {exc}")
    chosen = transport()
    if not chosen:
        # Configuration can arrive while the link is still good.
        return Delivery(RETRY, "mail is not configured")
    if chosen == "log":
        # as_bytes rather than as_string, which would turn an 8bit part back
        # into base64 and leave nothing in the log a person could read.
        logger.info("MAIL_TRANSPORT=log, not sent:\n%s", raw.decode("utf-8", "replace"))
        return Delivery(SENT, "written to the log")
    return _post(message)


def deliver(message: Message) -> Delivery:
    """Hand one message to the transport and say what became of it.

    Blocking, and never raises. The outbox runs it on a worker thread and acts
    on the outcome; an exception would leave the message in flight with nothing
    to say why.
    """
    try:
        delivery = _attempt(message)
    except Exception as exc:
        logger.exception("Mail to %r failed unexpectedly", message.to)
        delivery = Delivery(RETRY, f"unexpected {type(exc).__name__}")
    delivery = replace(delivery, detail=delivery.detail[:_MAX_DETAIL])

    if delivery.outcome == SENT:
        logger.debug("Mail to %r %s (%s)", message.to, delivery.detail, delivery.message_id or "no message id")
    elif delivery.outcome == RETRY and delivery.account_fault:
        # Every mail gets this answer until the setting is fixed, so the line
        # says what to fix and names no recipient. The outbox's own line for
        # the retry says which mail was held.
        logger.error(
            "Cloudflare refused the send because of the account, not the mail: %s. The token, the account or "
            "the sending domain needs fixing: check CLOUDFLARE_EMAIL_TOKEN and its Email Sending permission, "
            "CLOUDFLARE_ACCOUNT_ID, and that the domain of MAIL_FROM is onboarded for Email Sending. Mail is "
            "retried meanwhile, within its attempts and the life of its link.",
            delivery.detail,
        )
    elif delivery.outcome == RETRY:
        logger.warning("Mail to %r not sent, worth retrying: %s", message.to, delivery.detail)
    elif delivery.outcome == UNDELIVERABLE:
        logger.warning("Mail to %r is undeliverable: %s", message.to, delivery.detail)
    else:
        logger.error("Mail to %r refused: %s", message.to, delivery.detail)
    return delivery
