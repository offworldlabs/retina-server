"""Parsing shared by the modules that read list-valued environment variables.

This module must import nothing but the standard library.  ``core.users``
imports it, and ``migrations/env.py`` imports ``core.users`` to build
target_metadata, so whatever this module pulls in becomes part of the migration
environment's surface too.
"""


def parse_comma_list(raw: str) -> tuple[str, ...]:
    """Split a comma-separated list, stripping whitespace and dropping empty
    segments.  A blank segment (a leading, trailing or doubled comma) would
    otherwise become an entry that matches everything, e.g. a prefix that
    matches every node id via ``str.startswith("")``.

    Used for every list-valued environment variable, so the empty-segment rule
    holds in one place: CORS_ORIGINS, AUTH_ADMIN_EMAILS and
    NODE_FORCE_RETIRE_PREFIXES all parse through it.
    """
    return tuple(p.strip() for p in raw.split(",") if p.strip())
