"""The words and markup of the sign-in and claim mails, services/mail_templates.py.

The text parts are pinned word for word: they are what every client that will
not render HTML shows, and the claim mail's wording is a deliberate request
rather than a notification. The HTML is checked for what makes it safe to send:
every value escaped, no URL but the link, and nothing a mail client fetches.
"""

import html
import re
import subprocess
import sys
from pathlib import Path

import pytest

from services import mail_templates
from services.mail_templates import Rendered, claim, sign_in

BACKEND = Path(__file__).resolve().parents[1]

LINK = "https://app.retina.fm/auth/link/" + "Zq3x_9Kd-" * 5 + "?next=/nodes"
CLAIM_LINK = "https://app.retina.fm/auth/claim/" + "Zq3x_9Kd-" * 5
NODE_REF = "nde1a2b3c4d00"


def _visible(markup: str) -> str:
    """The text a reader sees: no head, no tags, entities decoded, whitespace collapsed."""
    body = markup.split("<body", 1)[1]
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def _styles(markup: str) -> list[str]:
    return re.findall(r'style="([^"]*)"', markup)


RENDERED = {
    "sign-in": lambda: sign_in(LINK),
    "claim": lambda: claim(NODE_REF, CLAIM_LINK, 15),
}


@pytest.fixture(params=sorted(RENDERED))
def rendered(request) -> tuple[Rendered, str]:
    """Each mail, with the link it was rendered with."""
    return RENDERED[request.param](), LINK if request.param == "sign-in" else CLAIM_LINK


# ── The text parts ────────────────────────────────────────────────────────────


class TestSignIn:
    def test_subject(self):
        assert sign_in(LINK).subject == "Sign in to RETINA"

    def test_text_is_the_wording_people_already_get(self):
        assert sign_in(LINK).text == (
            "A request has been made to sign in to RETINA with this email address.\n\n"
            "Click this link to log in:\n"
            f"{LINK}\n\n"
            "The link works once and expires in 15 minutes.\n\n"
            "If this wasn't you, you may safely ignore this email."
        )


class TestClaim:
    def test_subject_names_the_node(self):
        assert claim(NODE_REF, CLAIM_LINK, 15).subject == f"Connect your RETINA node {NODE_REF}"

    @pytest.mark.parametrize("minutes", [15, 60])
    def test_text_is_the_wording_people_already_get(self, minutes):
        assert claim(NODE_REF, CLAIM_LINK, minutes).text == (
            f"Someone entered this address while setting up a RETINA node, {NODE_REF}.\n\n"
            f"{CLAIM_LINK}\n\n"
            "Opening that link connects the node to this address and signs you in. The link works "
            f"once and expires in {minutes} minutes.\n\n"
            "If this wasn't you, somebody has mistyped their own address. The page will let you say "
            "so, and nothing about the node changes in the meantime."
        )

    def test_a_node_ref_stays_as_given_in_the_text(self):
        """Plain text needs no escaping, and escaping it would show entities."""
        hostile = '<script>alert(1)</script>"&'
        rendered = claim(hostile, CLAIM_LINK, 15)
        assert hostile in rendered.text
        assert rendered.subject == f"Connect your RETINA node {hostile}"


# ── The HTML ──────────────────────────────────────────────────────────────────


class TestHtml:
    def test_is_a_document_with_a_language_and_a_title(self, rendered):
        mail, _ = rendered
        assert mail.html.startswith("<!DOCTYPE html>\n")
        assert '<html lang="en"' in mail.html
        assert f"<title>{html.escape(mail.subject)}</title>" in mail.html

    def test_carries_the_link_as_a_button_and_as_text(self, rendered):
        """The text copy is for clients that strip the button."""
        mail, link = rendered
        assert mail.html.count(f'href="{link}"') == 2
        assert f">{link}</a>" in mail.html

    def test_has_no_url_but_the_link(self, rendered):
        """Nothing else to click, and nothing that reports the mail was opened."""
        mail, link = rendered
        assert set(re.findall(r"""(?i)\b[a-z][a-z0-9+.-]*://[^\s"'<>]*""", mail.html)) == {link}
        assert set(re.findall(r'(?i)\b(?:href|src|action|background)="([^"]*)"', mail.html)) == {link}

    @pytest.mark.parametrize(
        "fetch",
        [
            "<img",
            "<script",
            "<link",
            "<iframe",
            "<object",
            "<embed",
            "<form",
            "<video",
            "<audio",
            "url(",
            "@import",
            "@font-face",
            " src=",
            "mailto:",
        ],
    )
    def test_fetches_nothing(self, rendered, fetch):
        """No images, web fonts, external CSS or scripts: each is a remote
        fetch most clients block, and the rest use to track."""
        mail, _ = rendered
        assert fetch not in mail.html.lower()

    def test_uses_the_system_font_stack(self, rendered):
        mail, _ = rendered
        fonts = {match for style in _styles(mail.html) for match in re.findall(r"font-family:([^;]*)", style)}
        assert fonts == {mail_templates._FONT}
        assert "-apple-system" in mail_templates._FONT
        assert mail_templates._FONT.endswith("sans-serif")

    def test_lays_out_with_tables_and_inline_styles(self, rendered):
        mail, _ = rendered
        assert mail.html.count('<table role="presentation"') >= 3
        assert "<div" not in mail.html

    def test_sets_a_background_wherever_it_sets_a_colour_and_vice_versa(self, rendered):
        """A client that inverts for dark mode then inverts a pair, and the
        contrast survives. Text colour alone is fine: it sits on a background
        set further out."""
        mail, _ = rendered
        for style in _styles(mail.html):
            if "background-color:" in style:
                assert re.search(r"(^|;)color:", style), style
        body = re.search(r'<body[^>]*style="([^"]*)"', mail.html)
        assert body is not None
        assert "background-color:" in body.group(1)
        assert re.search(r"(^|;)color:", body.group(1))

    def test_declares_and_handles_dark_mode(self, rendered):
        mail, _ = rendered
        assert '<meta name="color-scheme" content="light dark">' in mail.html
        assert "@media (prefers-color-scheme: dark)" in mail.html

    def test_fits_a_phone(self, rendered):
        """390px wide: the viewport is the device's, the card is fluid up to a
        cap rather than fixed, and long words and the link wrap."""
        mail, _ = rendered
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in mail.html
        assert "max-width:520px" in mail.html
        assert not re.search(r'width="(?!100%|32")\d+', mail.html)
        assert "word-break:break-all" in mail.html
        assert "overflow-wrap:break-word" in mail.html

    def test_is_ascii_with_lines_that_fit_7bit(self, rendered):
        """What lets services/mail.py send the part 7bit, the encoding that
        never splits the link."""
        mail, _ = rendered
        assert mail.html.isascii()
        assert max(len(line) for line in mail.html.splitlines()) < 998

    def test_says_what_the_text_says(self, rendered):
        mail, link = rendered
        visible = _visible(mail.html)
        for paragraph in mail.text.split("\n\n"):
            for line in paragraph.splitlines():
                # The text's lead-in to a bare link is the button's job in HTML.
                if line != "Click this link to log in:":
                    assert " ".join(line.split()) in visible, line
        assert link in visible

    def test_the_buttons_say_what_they_do(self):
        assert ">Log in</a>" in sign_in(LINK).html
        # Open, not connect: the page it opens still asks, and offers declining.
        assert ">Open the link</a>" in claim(NODE_REF, CLAIM_LINK, 15).html


class TestEscaping:
    HOSTILE_REF = "<script>alert('x')</script>\"&amp;"

    def test_a_hostile_node_ref_is_shown_not_run(self):
        markup = claim(self.HOSTILE_REF, CLAIM_LINK, 15).html
        assert "<script" not in markup
        assert "alert('x')" not in markup
        escaped = html.escape(self.HOSTILE_REF, quote=True)
        assert f"<title>Connect your RETINA node {escaped}</title>" in markup
        assert markup.count(escaped) == 3  # title, heading, first paragraph
        assert f"Connect your RETINA node {self.HOSTILE_REF}" in _visible(markup)

    def test_the_link_cannot_break_out_of_its_attribute(self):
        hostile = 'https://app.retina.fm/auth/link/a" onmouseover="steal()&x=<b>'
        markup = sign_in(hostile).html
        escaped = "https://app.retina.fm/auth/link/a&quot; onmouseover=&quot;steal()&amp;x=&lt;b&gt;"
        assert markup.count(f'href="{escaped}"') == 2
        assert 'onmouseover="' not in markup
        assert "<b>" not in markup

    def test_a_query_string_ampersand_is_escaped_in_the_href(self):
        markup = sign_in("https://app.retina.fm/auth/link/t?next=/a&b=c").html
        assert 'href="https://app.retina.fm/auth/link/t?next=/a&amp;b=c"' in markup

    def test_the_minutes_are_escaped_too(self):
        """An int today; escaped all the same, so a caller passing a string
        cannot reach the markup through it."""
        markup = claim(NODE_REF, CLAIM_LINK, "<i>15</i>").html  # type: ignore[arg-type]
        assert "<i>" not in markup

    def test_non_ascii_becomes_character_references(self):
        """An escaped value stays ASCII, so the part stays 7bit."""
        rendered = claim("nöde ✈", CLAIM_LINK, 15)
        assert rendered.html.isascii()
        assert "n&#246;de &#9992;" in rendered.html
        assert "nöde ✈" in rendered.text
        assert "nöde ✈" in _visible(rendered.html)


def test_renders_with_no_environment_at_all():
    """A checker renders both mails to files with `python -c` and nothing set,
    and the outbox renders them whatever the deployment's settings are."""
    script = (
        "from services.mail_templates import claim, sign_in\n"
        "print(sign_in('https://app.retina.fm/auth/link/t').subject)\n"
        "print(claim('nde1', 'https://app.retina.fm/auth/claim/t', 15).subject)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, env={}, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["Sign in to RETINA", "Connect your RETINA node nde1"]
