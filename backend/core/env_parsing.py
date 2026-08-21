"""Parsing shared by the modules that read list-valued environment variables.

Deliberately dependency-free, importing nothing but the standard library, and
deliberately in ``core`` rather than ``config``.  ``migrations/env.py`` imports
``core.users`` to build target_metadata, so anything ``core.users`` imports is
pulled into the migration environment as well; homing this in ``config`` put
the whole constants module on that path for the sake of a four-line parse, and
the migration test's minimal-image fixture had to copy a third directory to
keep working.  Keep it free of project imports so that stays true.
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
