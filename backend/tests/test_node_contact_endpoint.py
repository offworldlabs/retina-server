"""PUT /v1/nodes/contact, through the app.

The store's own behaviour is pinned in test_node_contact_store.py. What is
tested here is what only the route can get wrong: the order of authentication
and validation, the wire shapes, and the commit.

Assertions read the database through a fresh query after the request rather than
off objects the handler left attached: the handler runs on its own loop and
thread, per node_client's docstring.
"""

import pytest
from sqlalchemy import select

from core.nodes import NodeContact

CONTACT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+44 20 7946 0000",
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _row(session, node_id: str) -> NodeContact | None:
    session.expire_all()
    result = await session.execute(select(NodeContact).where(NodeContact.node_id == node_id))
    return result.scalars().first()


async def test_a_contact_document_is_stored(registered_node, node_session, node_client):
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/contact", json=dict(CONTACT), headers=_auth(token))

    assert response.status_code == 200
    row = await _row(node_session, node_id)
    assert {field: getattr(row, field) for field in CONTACT} == CONTACT


async def test_the_response_carries_the_time_the_row_was_written(registered_node, node_client):
    token, _node_id = registered_node

    body = node_client.put("/v1/nodes/contact", json=dict(CONTACT), headers=_auth(token)).json()

    assert set(body) == {"updated_at"}
    assert body["updated_at"].endswith("Z")


async def test_an_empty_document_is_accepted_and_stores_nulls(registered_node, node_session, node_client):
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/contact", json={}, headers=_auth(token))

    assert response.status_code == 200
    row = await _row(node_session, node_id)
    assert (row.first_name, row.last_name, row.email, row.phone) == (None, None, None, None)


async def test_a_resend_clears_what_it_omits(registered_node, node_session, node_client):
    token, node_id = registered_node
    node_client.put("/v1/nodes/contact", json=dict(CONTACT), headers=_auth(token))

    node_client.put("/v1/nodes/contact", json={"first_name": "Ada"}, headers=_auth(token))

    row = await _row(node_session, node_id)
    assert (row.first_name, row.email) == ("Ada", None)


async def test_no_bearer_is_401_and_writes_nothing(registered_node, node_session, node_client):
    _token, node_id = registered_node

    response = node_client.put("/v1/nodes/contact", json=dict(CONTACT))

    assert response.status_code == 401
    assert await _row(node_session, node_id) is None


async def test_a_bad_bearer_is_401_rather_than_a_body_refusal(registered_node, node_client):
    """Identity resolves before the body, so a broken body behind a bad token is a 401."""
    response = node_client.put("/v1/nodes/contact", json={"middle_name": "Byron"}, headers=_auth("not-a-token"))

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"middle_name": "Byron"}, "middle_name"),
        ({"email": "ada"}, "email"),
        ({"phone": "call me"}, "phone"),
        ({"first_name": 42}, "first_name"),
        ({"last_name": "a" * 65}, "last_name"),
    ],
)
async def test_a_refused_document_names_the_field(registered_node, node_client, payload, field):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/contact", json=payload, headers=_auth(token))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_contact", "detail": field}


async def test_a_body_that_is_not_json_is_refused_the_same_way(registered_node, node_client):
    token, _node_id = registered_node

    response = node_client.put(
        "/v1/nodes/contact",
        content="not json",
        headers=_auth(token) | {"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_contact"


async def test_a_body_that_is_not_an_object_is_refused(registered_node, node_client):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/contact", json=["ada@example.com"], headers=_auth(token))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_contact", "detail": "contact"}
