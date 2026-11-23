"""The helpers owner-scoped route tests sign in with.

Proved on /api/auth/me/nodes, which answers from the caller's own session and
nothing else.
"""

import pytest

from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import own


@pytest.fixture
def real(monkeypatch):
    real_sessions(monkeypatch)


class TestRealSessions:
    def test_the_suite_bypass_answers_a_signed_out_caller_as_admin(self, client):
        """The control: without real_sessions a signed-out request is not refused."""
        assert client.get("/api/auth/me/nodes").status_code == 200

    def test_a_signed_out_caller_is_refused(self, client, real):
        r = client.get("/api/auth/me/nodes")
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    def test_the_flag_is_off_in_modules_that_imported_it_by_name(self, real):
        import routes.streaming

        assert routes.streaming.AUTH_BYPASS is False

    def test_the_flag_is_restored_after_the_test(self):
        import core.users
        import routes.streaming

        assert core.users.AUTH_BYPASS is True
        assert routes.streaming.AUTH_BYPASS is True


class TestSignIn:
    def test_a_signed_in_caller_is_answered(self, client, real):
        as_user(client, sign_in(client, "a@example.com"))
        r = client.get("/api/auth/me/nodes")
        assert r.status_code == 200
        assert r.json() == []

    def test_signing_in_does_not_act_as_the_account(self, client, real):
        sign_in(client, "a@example.com")
        assert client.get("/api/auth/me/nodes").status_code == 401

    def test_a_token_is_what_the_caller_acts_as(self, client, real):
        as_user(client, sign_in(client, "a@example.com"))
        assert client.get("/api/auth/me").json()["email"] == "a@example.com"

    def test_as_user_none_goes_back_to_nobody(self, client, real):
        as_user(client, sign_in(client, "a@example.com"))
        as_user(client, None)
        assert client.get("/api/auth/me/nodes").status_code == 401

    def test_two_addresses_are_two_users(self, client, real):
        sign_in(client, "a@example.com")
        sign_in(client, "b@example.com")
        a, b = user_id_of("a@example.com"), user_id_of("b@example.com")
        assert a != b
        assert user_id_of("A@Example.com") == a

    def test_a_second_sign_in_is_the_same_user(self, client, real):
        sign_in(client, "a@example.com")
        first = user_id_of("a@example.com")
        sign_in(client, "a@example.com")
        assert user_id_of("a@example.com") == first


class TestOwnership:
    def test_a_node_is_listed_for_its_owner_and_not_for_another_user(self, client, real):
        token_a = sign_in(client, "a@example.com")
        token_b = sign_in(client, "b@example.com")
        own("owner-helper-node", user_id_of("a@example.com"))

        as_user(client, token_a)
        assert [n["node_id"] for n in client.get("/api/auth/me/nodes").json()] == ["owner-helper-node"]

        as_user(client, token_b)
        assert client.get("/api/auth/me/nodes").json() == []
