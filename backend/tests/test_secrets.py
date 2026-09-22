import pytest
from cryptography.fernet import Fernet, InvalidToken

from core import secrets as polled_secrets


def test_a_secret_round_trips_under_the_key(monkeypatch):
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())

    token = polled_secrets.encrypt("hunter2")

    assert "hunter2" not in token
    assert polled_secrets.decrypt(token) == "hunter2"


def test_another_key_cannot_read_it(monkeypatch):
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())
    token = polled_secrets.encrypt("hunter2")

    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        polled_secrets.decrypt(token)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_encrypting_without_a_key_is_refused(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("POLLED_RADAR_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", value)

    with pytest.raises(polled_secrets.SecretKeyUnavailable, match="POLLED_RADAR_SECRET_KEY"):
        polled_secrets.encrypt("hunter2")


def test_an_invalid_key_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", "not-a-fernet-key")

    with pytest.raises(polled_secrets.SecretKeyUnavailable, match="POLLED_RADAR_SECRET_KEY"):
        polled_secrets.encrypt("hunter2")
