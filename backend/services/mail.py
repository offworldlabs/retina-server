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
"""

import logging
import os
import threading
from email.message import EmailMessage
from email.utils import parseaddr

import httpx

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/email/sending/send"

_TIMEOUT_S = 15.0

#: Concurrent delivery threads, over all recipients. nginx limits the credential
#: endpoints to 5r/m, but only on the three vhosts that carry a
#: `location /api/auth/`; the rest have a blanket `location /api/`, and a direct
#: caller has neither. Past this cap a send is dropped rather than queued: the
#: link would arrive after its own fifteen-minute expiry anyway, and the sender
#: is worth more than the backlog.
_MAX_INFLIGHT_SENDS = 16
_inflight = 0
_inflight_lock = threading.Lock()

#: `log` writes what would have been sent to the application log, for a laptop
#: with no Cloudflare token. It never resolves in production: a magic link in a
#: log file is a credential in a log file.
_TRANSPORTS = ("cloudflare", "log")


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


def transport() -> str:
    """The transport to use, or "" when mail is not usable as configured.

    Every reason for "" is checked here rather than at each call site, so
    is_configured() and send() cannot disagree about what is set up.
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


def _build(to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = _setting("MAIL_FROM")
    message["To"] = to
    message["Subject"] = subject
    try:
        # 7bit rather than the default: a sign-in URL runs past the 78 columns
        # that picks quoted-printable, and QP breaks it across two lines with a
        # soft `=`. A compliant client rejoins them, but a plain-text view or a
        # copy-paste hands the reader half a token, and the link is the whole
        # credential. Only valid while the body is ASCII, which the fallback
        # covers.
        message.set_content(body, cte="7bit")
    except (UnicodeEncodeError, ValueError):
        message.set_content(body)
    return message


def _post(to: str, subject: str, body: str) -> str | None:
    """Hand one message to Cloudflare: None when it was accepted, else why not."""
    name, address = parseaddr(_setting("MAIL_FROM"))
    sender = {"address": address, "name": name} if name else {"address": address}
    response = httpx.post(
        _SEND_URL.format(account=_setting("CLOUDFLARE_ACCOUNT_ID")),
        headers={"Authorization": f"Bearer {_setting('CLOUDFLARE_EMAIL_TOKEN')}"},
        json={"from": sender, "to": [to], "subject": subject, "text": body},
        timeout=_TIMEOUT_S,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if not response.is_success or not payload.get("success"):
        reasons = "; ".join(f"{e.get('code')} {e.get('message')}" for e in payload.get("errors") or [])
        return f"HTTP {response.status_code}: {reasons or response.reason_phrase}"
    # Both come back with a 200 and success: true, so neither shows in the status.
    result = payload.get("result") or {}
    for outcome in ("permanent_bounces", "suppressed_recipients"):
        if result.get(outcome):
            return outcome.replace("_", " ")
    return None


def send(to: str, subject: str, body: str) -> bool:
    """Deliver one message, returning whether it went.

    Never raises. The magic-link route answers identically whether or not mail
    went out, so an exception escaping here would turn the response into an
    oracle for which addresses exist.
    """
    chosen = transport()
    if not chosen:
        logger.warning("Mail is not configured; dropping message to %s (%r)", to, subject)
        return False

    try:
        # Built for both transports and inside the guard: EmailMessage's default
        # policy refuses a header value carrying a linefeed, which is how an
        # address like "someone@example.com\nBcc: ..." is stopped before the API
        # is handed it. It refuses by raising, so building outside would let a
        # hostile address answer differently from an unknown one.
        message = _build(to, subject, body)
        if chosen == "log":
            logger.info("MAIL_TRANSPORT=log, not sent:\n%s", message.as_string())
            return True
        failure = _post(to, subject, body)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Mail to %s failed: %s: %s", to, type(exc).__name__, exc)
        return False
    if failure:
        logger.error("Mail to %s failed: %s", to, failure)
        return False
    return True


def send_in_background(to: str, subject: str, body: str) -> None:
    """Hand the send to a daemon thread and return immediately.

    The caller's response time must not vary with whether an address was worth
    mailing, and the request is blocking. Failures are the thread's to log.
    """
    global _inflight

    with _inflight_lock:
        if _inflight >= _MAX_INFLIGHT_SENDS:
            logger.error("Dropping message to %s: %d sends already in flight", to, _inflight)
            return
        _inflight += 1

    def _deliver() -> None:
        global _inflight
        try:
            send(to, subject, body)
        finally:
            with _inflight_lock:
                _inflight -= 1

    try:
        threading.Thread(target=_deliver, daemon=True, name="mail-send").start()
    except Exception as exc:
        # The slot is only released by the thread, so a thread that never
        # started has to give it back here or the cap leaks down to zero.
        with _inflight_lock:
            _inflight -= 1
        logger.error("Could not start mail thread for %s: %s", to, exc)
