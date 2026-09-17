"""Outbound transactional mail over Cloudflare Email Service.

Authenticated SMTP rather than the Workers binding or the REST API: the backend
is not a Worker, `smtplib` reaches Cloudflare directly, and all three routes
enter the same pipeline with the same DKIM and ARC signing and the same
delivery logs.

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
import smtplib
import threading
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.mx.cloudflare.net"
SMTP_PORT = 465

#: Cloudflare authenticates the token, not an account, so the username is this
#: fixed string for every sender and the API token is the password.
SMTP_USERNAME = "api_token"

_SMTP_TIMEOUT_S = 15.0

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
_TRANSPORTS = ("smtp", "log")


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
    if chosen == "smtp" and not _setting("CLOUDFLARE_EMAIL_TOKEN"):
        logger.warning("CLOUDFLARE_EMAIL_TOKEN is not set; no mail will be sent.")
        return ""
    return chosen


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
    logger.info("Mail: %s via %s as %s", chosen, SMTP_HOST, sender)


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
        # Inside the guard: EmailMessage's default policy refuses a header
        # value carrying a linefeed, which is how an address like
        # "someone@example.com\nBcc: ..." is stopped. It refuses by raising, so
        # building outside would let a hostile address answer differently from
        # an unknown one.
        message = _build(to, subject, body)
        if chosen == "log":
            logger.info("MAIL_TRANSPORT=log, not sent:\n%s", message.as_string())
            return True
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=_SMTP_TIMEOUT_S) as server:
            server.login(SMTP_USERNAME, _setting("CLOUDFLARE_EMAIL_TOKEN"))
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # str(exc), not the traceback: an SMTPAuthenticationError's repr can
        # carry the credential it failed with.
        logger.error("Mail to %s failed: %s: %s", to, type(exc).__name__, exc)
        return False
    return True


def send_in_background(to: str, subject: str, body: str) -> None:
    """Hand the send to a daemon thread and return immediately.

    The caller's response time must not vary with whether an address was worth
    mailing, and `smtplib` is blocking. Failures are the thread's to log.
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
