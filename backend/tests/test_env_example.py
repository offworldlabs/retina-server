"""Every environment key the backend reads is an entry in backend/.env.example,
set or commented out, unless it is left out on purpose below. The keys come from
the source (tests/env_reads.py), so an `os.getenv("NEW_KEY")` fails here until
the example names it.
"""

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from tests.env_reads import Resolver, load

BACKEND = Path(__file__).resolve().parents[1]
EXAMPLE = BACKEND / ".env.example"

# Not the server: the suite sets its own environment, and scripts are tools run beside it.
NOT_THE_SERVER = {"tests", "scripts"}

# Set on the server service by docker-compose.yml, the same everywhere, or by an
# environment's overlay; check-env-parity.py compares staging's and test's with
# production's.
SET_BY_COMPOSE = {
    "CF_ACCESS_AUD",
    "CF_ACCESS_TEAM_DOMAIN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CORS_ORIGINS",
    "FRAME_QUEUE_SIZE",
    "FRAME_WORKERS",
    "HOST_APP",
    "KNOWN_LANE_MODE",
    "MAIL_FROM",
    "MAIL_TRANSPORT",
    "NODE_FRAME_MIN_INTERVAL_S",
    "RADAR_TCP_PORT",
    "SIM_FRAC_ANOMALOUS",
    "SIM_FRAC_DARK",
    "SIM_FRAC_DRONE",
    "TRACK_MAX_STALE_S",
}
# Set by the suite alone, to build its schema with create_all.
TEST_ONLY = {"RETINA_SCHEMA_SOURCE"}
# Older names still read, which the example describes beside the current one.
OLDER_NAMES = {"NODE_FUZZ_SITE_AUDIT_KM": "NODE_FUZZ_SITE_KM"}

LEFT_OUT = SET_BY_COMPOSE | TEST_ONLY | set(OLDER_NAMES)


@pytest.fixture(scope="module")
def reads():
    return Resolver(load(BACKEND, NOT_THE_SERVER)).reads()


@pytest.fixture(scope="module")
def read_keys(reads) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for read in reads:
        for key in read.keys:
            keys.setdefault(key, []).append(f"{read.module}:{read.line}")
    return keys


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (`!reset`, `!override`) read as the value they tag."""


def _untagged(loader, suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _server_environment(compose: Path) -> set[str]:
    server = (yaml.load(compose.read_text(), _ComposeLoader).get("services") or {}).get("server") or {}
    environment = server.get("environment") or []
    if isinstance(environment, dict):
        return set(environment)
    return {entry.split("=", 1)[0] for entry in environment}


def _entries() -> set[str]:
    """`KEY=value` lines, commented out or not, and not a sentence that starts with one."""
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=\S*[ \t]*(?:#.*)?$", EXAMPLE.read_text(), re.MULTILINE))


def test_every_key_the_backend_reads_is_in_the_example(read_keys):
    documented = _entries() | LEFT_OUT
    missing = {key: sites for key, sites in read_keys.items() if key not in documented}
    assert not missing, (
        "backend/.env.example has no entry for these, which the backend reads (add one, commented out "
        "if it should not be set by default):\n"
        + "\n".join(f"  {k}  ({', '.join(v)})" for k, v in sorted(missing.items()))
    )


def test_every_read_resolves_to_its_keys(reads):
    """A read whose key cannot be worked out would be skipped by the test
    above; this makes it fail instead."""
    unresolved = [f"{r.module}:{r.line}: {r.problem}" for r in reads if r.problem]
    assert not unresolved, (
        "cannot tell which environment key these read; pass a literal, a module constant, or a helper "
        "argument that is one:\n  " + "\n  ".join(unresolved)
    )


def test_the_keys_left_out_are_still_read_and_still_where_they_are_said_to_be(read_keys):
    assert not LEFT_OUT - set(read_keys), (
        f"no longer read, so no longer an exception: {sorted(LEFT_OUT - set(read_keys))}"
    )
    set_on_server = set().union(*(_server_environment(p) for p in BACKEND.parent.glob("docker-compose*.yml")))
    unset = sorted(SET_BY_COMPOSE - set_on_server)
    assert not unset, f"said to be set by compose, but no docker-compose*.yml sets them on the server: {unset}"
    conftest = (BACKEND / "tests" / "conftest.py").read_text()
    assert all(k in conftest for k in TEST_ONLY), "a test-only key the suite no longer sets"
    entries, example = _entries(), EXAMPLE.read_text()
    for older, current in OLDER_NAMES.items():
        assert current in entries and older in example, f"{older} is not described beside {current}"


# ── the reader itself ─────────────────────────────────────────────────────────


def _read(tmp_path: Path, sources: dict[str, str]) -> tuple[set[str], list[str]]:
    for name, source in sources.items():
        path = tmp_path / f"{name.replace('.', '/')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    reads = Resolver(load(tmp_path, set())).reads()
    return {k for r in reads for k in r.keys}, [r.problem for r in reads if r.problem]


def test_the_reader_follows_every_indirection_the_backend_uses(tmp_path):
    keys, problems = _read(
        tmp_path,
        {
            "names": """
                ENABLED_ENV = "BY_CONSTANT"
                SHARED = {"a": "IN_A_DICT"}
            """,
            "app": """
                import os
                import os as _os
                from collections.abc import Mapping
                from names import ENABLED_ENV, SHARED

                LITERAL = os.getenv("LITERAL")
                ALIASED = _os.getenv("ALIASED")
                SUBSCRIPT = os.environ["SUBSCRIPT"]
                PRESENT = "PRESENT" in os.environ
                IMPORTED = os.environ.get(ENABLED_ENV)

                def _setting(name):
                    return os.getenv(name, "")

                MAIL = _setting("THROUGH_A_HELPER")
                for name in ("IN_A_LOOP",):
                    _setting(name)

                def _frac(name):
                    return os.environ.get(name)

                FRACS = {k: _frac(f"SIM_{k.upper()}") for k in {"dark": 0.1}}

                for key, var in SHARED.items():
                    os.environ.get(var)

                def flags(env: Mapping):
                    return env.get("THROUGH_A_MAPPING")

                flags(os.environ)

                def configure(env=None):
                    env = os.environ if env is None else env
                    return env.get("THROUGH_A_LOCAL")

                class Reader:
                    def flags(self, env):
                        return env.get("THROUGH_A_METHOD")

                Reader().flags(os.environ)

                def keyword(*, env):
                    return env["THROUGH_A_KEYWORD"]

                keyword(env=os.environ)

                NAME = "SHADOWED"

                def shadowing():
                    NAME = "LOCAL_CONSTANT"
                    return os.getenv(NAME)

                def annotated():
                    env: Mapping = os.environ.copy()
                    return env.get("THROUGH_A_COPY")

                def defaulted(env=None):
                    env = env or os.environ
                    return env.get("THROUGH_OR")

                def merged():
                    env = {**os.environ, "EXTRA": "1"}
                    return env.get("THROUGH_A_MERGE")
            """,
            "aliases": """
                import os.path
                from os import environ as ENVIRON

                ENV = os.environ
                _get = os.getenv
                ENV.get("THROUGH_A_MODULE_ALIAS")
                _get("THROUGH_A_GETENV_ALIAS")
                ENVIRON["FROM_OS_IMPORT"]
                dict(os.environ).get("THROUGH_DICT")
            """,
        },
    )
    assert not problems
    assert keys == {
        "LITERAL",
        "ALIASED",
        "SUBSCRIPT",
        "PRESENT",
        "BY_CONSTANT",
        "THROUGH_A_HELPER",
        "IN_A_LOOP",
        "SIM_DARK",
        "IN_A_DICT",
        "THROUGH_A_MAPPING",
        "THROUGH_A_LOCAL",
        "THROUGH_A_METHOD",
        "THROUGH_A_KEYWORD",
        "LOCAL_CONSTANT",
        "THROUGH_A_COPY",
        "THROUGH_A_MODULE_ALIAS",
        "THROUGH_A_GETENV_ALIAS",
        "FROM_OS_IMPORT",
        "THROUGH_DICT",
        "THROUGH_OR",
        "THROUGH_A_MERGE",
    }


@pytest.mark.parametrize(
    "source",
    [
        "import os\nprefix = input()\nos.getenv(prefix + '_KEY')\n",
        "import os\ndef _s(name):\n    name = 'RETINA_' + name\n    return os.getenv(name)\n_s('TIMEOUT')\n",
        "import os\nname = 'WRONG'\nget = lambda name: os.getenv(name)\nget('RIGHT')\n",
        "import os\nX = 'K'\nos.getenv(f'{X!r}')\n",
        "import os\nBASE = {'A': 1}\nfor k in {**BASE, 'B': 2}:\n    os.getenv(k)\n",
        "import os\nfor name in ('A', 'B'):\n    name = f'PREFIX_{name}'\n    os.getenv(name)\n",
    ],
    ids=["computed", "reassigned-parameter", "lambda-parameter", "converted", "spread", "reassigned-in-loop"],
)
def test_the_reader_reports_a_key_it_cannot_follow(tmp_path, source):
    keys, problems = _read(tmp_path, {"app": source})
    assert problems and not keys


def test_the_reader_resolves_names_where_python_would(tmp_path):
    """A relative import finds its sibling, not a top-level module of the same
    name, and a same-named function elsewhere is not the helper."""
    keys, problems = _read(
        tmp_path,
        {
            "c": "K = 'WRONG'\n",
            "pkg.__init__": "",
            "pkg.c": "K = 'RIGHT'\n",
            "pkg.app": "import os\nfrom .c import K\nos.getenv(K)\n",
            "a": "import os\ndef _s(name):\n    return os.getenv(name)\n_s('REAL')\n",
            "b": "def _s(name):\n    return name\n_s('NOT_ENV')\n",
            "svc.__init__": "",
            "svc.mail": "import os\ndef _m(name):\n    return os.getenv(name)\n_m('LOCAL')\n",
            "via_module": "from svc import mail\nmail._m('VIA_MODULE')\n",
            "svc.user": "from . import mail\nmail._m('VIA_RELATIVE')\n",
            "flags.__init__": "def flag(env):\n    return env.get('IN_A_PACKAGE')\n",
            "uses_flags": "import os\nfrom flags import flag\nflag(os.environ)\n",
            "same_name": "import os\ndef get(name):\n    return os.getenv(name)\nget('OWN')\n{}.get('NOT_ENV_EITHER')\n",
            "as_alias": "from svc.mail import _m as setting\nsetting('VIA_ALIAS')\n",
            "svc2.__init__": "from svc.mail import _m\n",
            "via_reexport": "from svc2 import _m\n_m('VIA_REEXPORT')\n",
            "via_package": "import os\nimport flags\nflags.flag(os.environ)\n",
            "reader": "class Reader:\n    def flags(self, env):\n        return env.get('XMOD_METHOD')\n\n    def get(self, name):\n        return name\n",
            "uses_reader": "import os\nfrom reader import Reader\nReader().flags(os.environ)\n{}.get('NOT_A_KEY')\n",
            "methods": """
                import os

                class A:
                    def read(self, name):
                        return os.getenv(name)

                    def use(self):
                        return self.read("A_KEY")

                class B:
                    def read(self, name):
                        return name

                    def use(self):
                        return self.read("NOT_A_KEY_EITHER")

                class Cfg:
                    def load(self):
                        def flags(env):
                            return env.get("NESTED_IN_A_METHOD")
                        return flags(os.environ)

                def setting(key=None):
                    if not key:
                        key = "DEFAULT_KEY"
                    return os.getenv(key)

                setting("CALLER_KEY")
            """,
            "shadowed": """
                import os
                env = os.environ

                def f(env):
                    return env.get("SHADOWED_NOT_ENV")

                f({})
            """,
            "more_scopes": """
                import os
                ENV = os.environ
                E2 = ENV
                E2.get("CHAINED")
                KEY = "FIRST"
                KEY = "SECOND"
                os.getenv(KEY)

                def configure(env=None):
                    env = os.environ if env is None else env
                    def get(k):
                        return env.get(k)
                    return get("CLOSURE")

                def local_alias():
                    g = os.getenv
                    return g("LOCAL_GETENV_ALIAS")
            """,
            "scopes": """
                import os
                KEY = "MODULE"
                NAMES = ("MODULE_A",)
                try:
                    _get = os.getenv
                except ImportError:
                    pass

                def outer():
                    KEY = "ENCLOSING"
                    def inner():
                        return os.getenv(KEY)
                    return inner

                def nested():
                    def inner():
                        KEY = "INNER"
                        return KEY
                    return os.getenv(KEY)

                def local_collection():
                    NAMES = ("LOCAL_B",)
                    for n in NAMES:
                        os.getenv(n)

                _get("IN_TRY")
            """,
        },
    )
    assert not problems
    assert keys == {
        "RIGHT",
        "REAL",
        "LOCAL",
        "VIA_MODULE",
        "VIA_RELATIVE",
        "IN_A_PACKAGE",
        "OWN",
        "ENCLOSING",
        "MODULE",
        "LOCAL_B",
        "IN_TRY",
        "VIA_ALIAS",
        "VIA_REEXPORT",
        "XMOD_METHOD",
        "CHAINED",
        "FIRST",
        "SECOND",
        "CLOSURE",
        "LOCAL_GETENV_ALIAS",
        "A_KEY",
        "NESTED_IN_A_METHOD",
        "DEFAULT_KEY",
        "CALLER_KEY",
    }


def test_the_reader_reports_a_helper_passed_as_a_value(tmp_path):
    """Its calls through map or partial cannot be found, so its keys cannot either."""
    _, problems = _read(
        tmp_path,
        {"app": "import os\ndef _s(name):\n    return os.getenv(name)\n_s('DIRECT')\nlist(map(_s, ['VIA_MAP']))\n"},
    )
    assert problems == ["_s is passed as a value in app"]


def test_a_mapping_no_call_fills_from_the_environment_is_not_read(tmp_path):
    keys, _ = _read(tmp_path, {"app": "def f(env):\n    return env.get('NOT_ENV')\n\nf({})\n"})
    assert keys == set()
