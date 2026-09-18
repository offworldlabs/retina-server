"""Putting a claim link in front of the person it was minted for.

What is tested here is the URL the mail carries, which nothing else in the
suite sees: every route test replaces `deliver` so it can count sends.
"""

import pytest

from services import claim_links, mail

NODE_REF = "nde1a2b3c4d00"


@pytest.fixture(autouse=True)
def _mail_configured(monkeypatch):
    monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "t")
    monkeypatch.setenv("HOST_APP", "app.retina.fm")
    monkeypatch.delenv("FORCE_HTTPS", raising=False)


@pytest.fixture
def sent(monkeypatch):
    posted = []
    monkeypatch.setattr(mail, "send_in_background", lambda to, subject, body: posted.append((to, subject, body)))
    return posted


async def test_the_link_lands_on_the_dashboard_claim_page(sent):
    """HOST_APP serves the map bundle at `/` and mounts the dashboard under
    `/dash/`, so a link without that prefix renders the map and never redeems."""
    assert await claim_links.deliver("ada@example.com", NODE_REF, "a-token") is True

    [(to, subject, body)] = sent
    assert to == "ada@example.com"
    assert NODE_REF in subject
    assert "https://app.retina.fm/dash/auth/claim/a-token" in body


async def test_the_link_follows_force_https(sent, monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "false")

    await claim_links.deliver("ada@example.com", NODE_REF, "a-token")

    assert "http://app.retina.fm/dash/auth/claim/a-token" in sent[0][2]


async def test_nothing_is_sent_without_a_host_to_address_the_link_to(sent, monkeypatch):
    """The request came from a node, so there is no browser host to fall back
    on, and a Host header is not one to trust with a credential anyway."""
    monkeypatch.delenv("HOST_APP", raising=False)

    assert await claim_links.deliver("ada@example.com", NODE_REF, "a-token") is False
    assert sent == []
