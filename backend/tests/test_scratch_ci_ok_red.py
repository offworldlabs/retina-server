"""Scratch only, never merged: a deliberate failure so ci-ok has a red verdict
to hold across a title edit."""

import pytest


def test_fails_on_purpose():
    pytest.fail("deliberate failure on the scratch PR proving ci-ok")
