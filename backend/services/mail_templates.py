"""What the mail this server sends says, as plain text and as HTML.

Every mail, one layout. The HTML is written for mail clients rather than
browsers: tables for structure and inline styles, because Gmail and Outlook
drop or rewrite most of what a web page relies on; the system font stack,
because a web font is a remote fetch most clients block; no images, for the
same reason; and no URL but the link itself, so there is nothing else to click
and nothing that reports the mail was opened. Text and background colours are
both set wherever either is, so a client that inverts for dark mode inverts a
pair and keeps the contrast; clients that honour prefers-color-scheme get the
console's own dark palette instead.

The link sits in a button and again as text, for clients that strip the button.
A mail with nothing to open, either notice about an account's address, has
neither.
Every interpolated value goes through _esc, the href included.

Nothing here reads the environment, so every mail renders anywhere.
"""

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str


# The console's palette (packages/shared/css/tokens.css), light and dark. The
# light accent is the console's hover shade, the lightest blue that keeps white
# button text above 4.5:1.
_PAGE, _PAGE_DARK = "#f1f5f9", "#0d1b2a"
_CARD, _CARD_DARK = "#ffffff", "#132240"
_BORDER, _BORDER_DARK = "#e2e8f0", "#1e365b"
_INK, _INK_DARK = "#0f172a", "#e2e8f0"
_SOFT, _SOFT_DARK = "#475569", "#94a3b8"
_ACCENT, _ACCENT_DARK = "#2563eb", "#38bdf8"
_ACCENT_INK, _ACCENT_INK_DARK = "#ffffff", "#082f49"
_LINK_DARK = "#7dd3fc"

_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

# Long words, an address or a node ref, wrap rather than widen a phone's card.
_WRAP = "overflow-wrap:break-word;word-break:break-word;"

_STYLE = f"""<style>
:root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
@media (prefers-color-scheme: dark) {{
  .page {{ background-color: {_PAGE_DARK} !important; color: {_INK_DARK} !important; }}
  .card {{ background-color: {_CARD_DARK} !important; border-color: {_BORDER_DARK} !important; }}
  .ink {{ color: {_INK_DARK} !important; }}
  .soft {{ color: {_SOFT_DARK} !important; }}
  .rule {{ border-color: {_BORDER_DARK} !important; }}
  .link {{ color: {_LINK_DARK} !important; }}
  .accent {{ background-color: {_ACCENT_DARK} !important; color: {_ACCENT_INK_DARK} !important; }}
  .accent a {{ color: {_ACCENT_INK_DARK} !important; }}
}}
@media (max-width: 480px) {{
  .pad {{ padding: 24px 20px !important; }}
}}
</style>"""


def _esc(value: object) -> str:
    """`value` as HTML text or attribute content, in ASCII.

    html.escape covers &, <, > and both quotes; anything past ASCII becomes a
    character reference. An ASCII part goes out as 7bit, which is what keeps
    the link in it on one line rather than broken by quoted-printable.
    """
    return html.escape(str(value), quote=True).encode("ascii", "xmlcharrefreplace").decode("ascii")


def _paragraph(text: str) -> str:
    return (
        f'<p class="ink" style="margin:0 0 16px;font-family:{_FONT};font-size:16px;line-height:24px;'
        f'color:{_INK};{_WRAP}">{_esc(text)}</p>'
    )


def _button(action: str, link: str) -> list[str]:
    """The link as a button, then written out beneath it."""
    href = _esc(link)
    return [
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 16px;">',
        "<tr>",
        f'<td class="accent" align="center" bgcolor="{_ACCENT}" style="border-radius:8px;'
        f'background-color:{_ACCENT};color:{_ACCENT_INK};">',
        f'<a href="{href}" target="_blank"',
        f' style="display:inline-block;padding:12px 28px;font-family:{_FONT};font-size:16px;line-height:24px;'
        f'font-weight:600;color:{_ACCENT_INK};text-decoration:none;border-radius:8px;">{_esc(action)}</a>',
        "</td>",
        "</tr>",
        "</table>",
        f'<p class="soft" style="margin:0 0 16px;font-family:{_FONT};font-size:14px;line-height:22px;'
        f'color:{_SOFT};">{_esc("Or copy this link into your browser:")}<br>',
        f'<a class="link" href="{href}" target="_blank"',
        f' style="color:{_ACCENT};text-decoration:underline;word-break:break-all;">{href}</a></p>',
    ]


def _layout(*, title: str, lead: str, action: str | None, link: str | None, follow: str, aside: str) -> str:
    """The page every mail shares: heading, lead, button, the link as text,
    what the link does, and an aside for whoever did not ask for the mail. A
    mail with no link has no button and no link written out."""
    button = _button(action, link) if action is not None and link is not None else []
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en" dir="ltr">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="x-apple-disable-message-reformatting">',
            '<meta name="format-detection" content="telephone=no, date=no, address=no, email=no">',
            '<meta name="color-scheme" content="light dark">',
            '<meta name="supported-color-schemes" content="light dark">',
            f"<title>{_esc(title)}</title>",
            _STYLE,
            "</head>",
            f'<body class="page" style="margin:0;padding:0;width:100%;background-color:{_PAGE};color:{_INK};">',
            f'<table role="presentation" class="page" width="100%" cellpadding="0" cellspacing="0" border="0"'
            f' style="background-color:{_PAGE};color:{_INK};">',
            "<tr>",
            '<td align="center" style="padding:32px 16px;">',
            f'<table role="presentation" class="card" width="100%" cellpadding="0" cellspacing="0" border="0"'
            f' style="max-width:520px;background-color:{_CARD};color:{_INK};border:1px solid {_BORDER};'
            'border-radius:12px;">',
            "<tr>",
            f'<td class="pad" style="padding:32px;font-family:{_FONT};color:{_INK};text-align:left;">',
            # The console's mark: the fisheye on an accent tile, then the name.
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;">',
            "<tr>",
            f'<td class="accent" width="32" height="32" align="center" valign="middle" style="width:32px;'
            f"height:32px;border-radius:8px;background-color:{_ACCENT};color:{_ACCENT_INK};"
            f'font-family:{_FONT};font-size:18px;line-height:32px;">&#9673;</td>',
            f'<td class="ink" style="padding:0 0 0 10px;font-family:{_FONT};font-size:14px;line-height:32px;'
            f'font-weight:700;letter-spacing:2px;color:{_INK};">RETINA</td>',
            "</tr>",
            "</table>",
            f'<h1 class="ink" style="margin:0 0 16px;font-family:{_FONT};font-size:22px;line-height:30px;'
            f'font-weight:700;color:{_INK};{_WRAP}">{_esc(title)}</h1>',
            _paragraph(lead),
            *button,
            _paragraph(follow),
            f'<p class="soft rule" style="margin:24px 0 0;padding:20px 0 0;border-top:1px solid {_BORDER};'
            f'font-family:{_FONT};font-size:14px;line-height:22px;color:{_SOFT};{_WRAP}">{_esc(aside)}</p>',
            "</td>",
            "</tr>",
            "</table>",
            "</td>",
            "</tr>",
            "</table>",
            "</body>",
            "</html>",
            "",
        ]
    )


def sign_in(link: str) -> Rendered:
    """The mail that carries a sign-in link."""
    subject = "Sign in to RETINA"
    asked = "A request has been made to sign in to RETINA with this email address."
    expiry = "The link works once and expires in 15 minutes."
    ignore = "If this wasn't you, you may safely ignore this email."
    text = f"{asked}\n\nClick this link to log in:\n{link}\n\n{expiry}\n\n{ignore}"
    markup = _layout(title=subject, lead=asked, action="Log in", link=link, follow=expiry, aside=ignore)
    return Rendered(subject, text, markup)


def claim(node_ref: str, link: str, expires_in_minutes: int, *, former: str | None = None) -> Rendered:
    """A request naming the node, with a decline, rather than a notification
    that assumes consent.

    Whoever receives this may have nothing to do with the node: anyone who can
    get one registered can make this server write to an address of their
    choosing. So it reads as somebody asking, it says what happens if they
    ignore it, and the page it opens offers declining as plainly as accepting.
    The button says what it does, open the link, rather than urging a connect
    the page itself still asks about.

    `former` is the address the node was given when it is one this account
    signed in with before, and the mail has come to the account's current
    address instead. It names that address, which is the account's own, so the
    reader knows why a node they never gave this address to is asking.
    """
    subject = f"Connect your RETINA node {node_ref}"
    if former is None:
        asked = f"Someone entered this address while setting up a RETINA node, {node_ref}."
        joins = "this address"
        mistyped = "somebody has mistyped their own address"
    else:
        asked = (
            f"Someone entered {former}, an address your RETINA account used to sign in with, while setting "
            f"up a RETINA node, {node_ref}."
        )
        joins = "your account"
        mistyped = "somebody has typed an address that used to be yours"
    opening = (
        f"Opening that link connects the node to {joins} and signs you in. The link works "
        f"once and expires in {expires_in_minutes} minutes."
    )
    mistyped = (
        f"If this wasn't you, {mistyped}. The page will let you say so, and nothing about the node "
        "changes in the meantime."
    )
    text = f"{asked}\n\n{link}\n\n{opening}\n\n{mistyped}"
    markup = _layout(title=subject, lead=asked, action="Open the link", link=link, follow=opening, aside=mistyped)
    return Rendered(subject, text, markup)


def address_change(link: str, expires_in_minutes: int) -> Rendered:
    """The mail to an address an account is moving to, carrying the link that
    confirms it.

    Whoever receives this may be a stranger whose address was mistyped, so it
    names nothing of the account, the address it signs in with now least of
    all, and says that ignoring it changes nothing.
    """
    subject = "Confirm your new RETINA address"
    asked = "A request has been made to make this the address a RETINA account signs in with."
    opening = (
        "Opening that link confirms the change. The account then signs in with this address. The link works "
        f"once and expires in {expires_in_minutes} minutes."
    )
    ignore = "If this wasn't you, you may safely ignore this email: nothing changes unless the link is opened."
    text = f"{asked}\n\nClick this link to confirm it:\n{link}\n\n{opening}\n\n{ignore}"
    markup = _layout(title=subject, lead=asked, action="Confirm the address", link=link, follow=opening, aside=ignore)
    return Rendered(subject, text, markup)


def address_changed() -> Rendered:
    """The notice to the address an account has just moved away from.

    It does not name the new address. The old one may be read by somebody else
    by now, which may be why it was changed, and the new address is the one
    thing that would tell them where the account went. It carries no link
    either: this address no longer signs in to anything.
    """
    subject = "Your RETINA address was changed"
    told = (
        "The RETINA account that signed in with this address now signs in with a different one, so this "
        "address no longer signs in to it."
    )
    kept = "The account's nodes and everything else on it are as they were."
    aside = "If you didn't make this change, contact a RETINA administrator."
    text = f"{told}\n\n{kept}\n\n{aside}"
    markup = _layout(title=subject, lead=told, action=None, link=None, follow=kept, aside=aside)
    return Rendered(subject, text, markup)


def address_taken() -> Rendered:
    """The notice to an address another account asked to move to, sent instead
    of the link that would confirm it, because it signs in to an account already.

    It names nothing of the account that asked: the reader may be a stranger to
    it, and the request may be somebody fishing for whose the address is, which
    is why that account is told nothing either. It carries no link, as there is
    nothing to confirm.
    """
    subject = "Your address was not added to another RETINA account"
    asked = "A request has been made to make this the address another RETINA account signs in with."
    refused = (
        "This address already signs in to a RETINA account, so it cannot be added to another one. Nothing has changed."
    )
    ignore = "If this wasn't you, you may safely ignore this email."
    text = f"{asked}\n\n{refused}\n\n{ignore}"
    markup = _layout(title=subject, lead=asked, action=None, link=None, follow=refused, aside=ignore)
    return Rendered(subject, text, markup)
