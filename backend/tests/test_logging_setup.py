"""Whether the server's INFO lines reach its log, and whether LOG_LEVEL decides it.

main.py configures the root logger, and the level it ends at decides whether any
INFO line from the backend is written: the boot lines, and the mail that
MAIL_TRANSPORT=log writes to the log in place of sending it. That configuration
used to follow the app imports, and a module that logs through the root logger
while it is imported (routes/radar.py and services/tcp_handler.py both do, when
the keys they check are unset) configures it first, at WARNING. main.py's own
call then did nothing, so no INFO line was written and LOG_LEVEL was ignored.

With INFO written, the libraries that log their own traffic have to be held
back: httpx alone writes a line for every request the server makes, with its
full URL, a registered radar's address and an alert webhook's ids among them.

Each case boots main in a child interpreter the way `uvicorn main:app` does,
uvicorn's own logging configuration first, then the import. A child because the
outcome is settled while main is imported, and pytest's interpreter imported it
long ago, under a log capture main.py leaves alone.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.migration_helpers import BACKEND
from tests.probe_helpers import PROBE_PREFIX

_PROBE = """
import json
import logging
import os

import dotenv
from uvicorn.config import Config

# main.py's load_dotenv() reads the test's file in place of backend/.env, so a
# developer's own settings cannot decide the outcome.
_load_dotenv = dotenv.load_dotenv
dotenv.load_dotenv = lambda *args, **kwargs: _load_dotenv(os.environ["PROBE_DOTENV"])

Config("main:app", log_level=os.environ.get("PROBE_UVICORN_LOG_LEVEL") or None).load()

import http.server
import threading

import httpx

mail = logging.getLogger("services.mail")
mail.info("probe: an info line")
mail.warning("probe: a warning line")
mail.error("probe: an error line")


class _Radar(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


# A request like the poller's, to a server of the probe's own. trust_env is off
# so that a proxy in the developer's environment cannot take the request.
server = http.server.HTTPServer(("127.0.0.1", 0), _Radar)
threading.Thread(target=server.serve_forever, daemon=True).start()
with httpx.Client(trust_env=False) as client:
    status = client.get(f"http://127.0.0.1:{server.server_port}{os.environ['PROBE_REQUEST_PATH']}").status_code
server.shutdown()
logging.getLogger("httpx").warning("probe: an httpx warning")

print("PROBE:" + json.dumps({
    "level": logging.getLevelName(mail.getEffectiveLevel()),
    "status": status,
    "libraries": {
        name: logging.getLevelName(logging.getLogger(name).getEffectiveLevel())
        for name in json.loads(os.environ["PROBE_LIBRARIES"])
    },
}))
"""

# Named for a radar's own path, so a line that carried the URL would show it.
REQUEST_PATH = "/probe-radar-7f3a/api/detection"
LIBRARIES = ("httpx", "httpcore", "urllib3", "botocore", "boto3", "s3transfer", "aiosqlite")

# The local stack (`just up`, the worktree stacks), and a box with RETINA_ENV
# unset, which core/users.py treats as production and so asks for a JWT_SECRET.
# RADAR_API_KEY and RADAR_NODE_TOKEN are unset in both, which is what has a
# module log through the root logger as it is imported: routes/radar.py in the
# first, services/tcp_handler.py (imported earlier) in the second. Production's
# uvicorn runs with `--log-level warning` (deploy/start.sh), which governs
# uvicorn's own loggers and must leave the app's alone.
LOCAL_STACK = {
    "RETINA_ENV": "dev",
    "RADAR_TCP_PORT": "0",
    "MAIL_TRANSPORT": "log",
    "MAIL_FROM": "retina@example.com",
    "HOST_APP": "localhost:5182",
    "FORCE_HTTPS": "false",
}
PRODUCTION_LIKE = {"JWT_SECRET": "0" * 32, "PROBE_UVICORN_LOG_LEVEL": "warning"}
ENVIRONMENTS = pytest.mark.parametrize("environment", [LOCAL_STACK, PRODUCTION_LIKE], ids=["local", "production"])

# Taken out of the suite's environment (conftest.py sets several of them, and
# a developer's shell may set the rest) so that each case says all of them.
_DECIDING = (
    "RETINA_ENV",
    "RADAR_API_KEY",
    "RADAR_NODE_TOKEN",
    "LOG_LEVEL",
    "AUTH_ALLOW_ANONYMOUS_ADMIN",
    "SYNTHETIC_FLEET_ENABLED",
)


def _probe(tmp_path, environment: dict[str, str], dotenv: str = "") -> tuple[dict, str]:
    """Boot main as uvicorn would, make one request with httpx; return what the probe reported and what was logged."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(dotenv)
    env = {k: v for k, v in os.environ.items() if k not in _DECIDING}
    env |= {
        "RETINA_DB_PATH": str(tmp_path / "users.db"),
        "PROBE_DOTENV": str(dotenv_path),
        "PROBE_REQUEST_PATH": REQUEST_PATH,
        "PROBE_LIBRARIES": json.dumps(LIBRARIES),
    } | environment
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=BACKEND, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(PROBE_PREFIX)), None)
    assert line is not None, result.stdout + result.stderr
    # The error line is the control: it shows the log was where the probe looked.
    assert "probe: an error line" in result.stderr, result.stderr
    report = json.loads(line.removeprefix(PROBE_PREFIX))
    # The request was answered, so a line about it had something to say.
    assert report["status"] == 200, report
    return report, result.stderr


def _boot(tmp_path, environment: dict[str, str], dotenv: str = "") -> tuple[str, str]:
    """Boot main as uvicorn would; return the effective level of services.mail and what was logged."""
    report, log = _probe(tmp_path, environment, dotenv)
    assert "probe: a warning line" in log, log
    return report["level"], log


@ENVIRONMENTS
def test_info_lines_reach_the_log_by_default(tmp_path, environment):
    level, log = _boot(tmp_path, environment)
    assert level == "INFO"
    assert "probe: an info line" in log


@ENVIRONMENTS
def test_log_level_in_the_environment_is_honoured(tmp_path, environment):
    level, log = _boot(tmp_path, environment | {"LOG_LEVEL": "WARNING"})
    assert level == "WARNING"
    assert "probe: an info line" not in log


def test_log_level_in_backend_env_is_honoured(tmp_path):
    """backend/.env is loaded after the imports, so its LOG_LEVEL arrives after
    the root logger has been configured and has to be applied again."""
    level, log = _boot(tmp_path, LOCAL_STACK, dotenv="LOG_LEVEL=WARNING\n")
    assert level == "WARNING"
    assert "probe: an info line" not in log


def test_a_log_level_that_names_no_level_logs_at_info_and_says_so(tmp_path):
    level, log = _boot(tmp_path, LOCAL_STACK | {"LOG_LEVEL": "LOUD"})
    assert level == "INFO"
    assert "probe: an info line" in log
    assert "LOG_LEVEL=LOUD" in log


@ENVIRONMENTS
def test_a_request_the_server_makes_is_not_logged_at_info(tmp_path, environment):
    report, log = _probe(tmp_path, environment)
    assert report["level"] == "INFO"
    assert "probe: an info line" in log
    assert "HTTP Request:" not in log
    assert REQUEST_PATH not in log
    assert report["libraries"] == dict.fromkeys(LIBRARIES, "WARNING")
    # Held back, not cut off: what goes wrong in a library still reaches the log.
    assert "probe: an httpx warning" in log


def test_log_level_debug_leaves_the_libraries_at_warning(tmp_path):
    report, log = _probe(tmp_path, PRODUCTION_LIKE | {"LOG_LEVEL": "DEBUG"})
    assert report["level"] == "DEBUG"
    assert "HTTP Request:" not in log
    assert REQUEST_PATH not in log
    assert report["libraries"] == dict.fromkeys(LIBRARIES, "WARNING")


def test_a_log_level_above_warning_holds_the_libraries_there_too(tmp_path):
    """From backend/.env, so the libraries follow the level applied after the
    imports and not only the one applied before them."""
    report, log = _probe(tmp_path, PRODUCTION_LIKE, dotenv="LOG_LEVEL=ERROR\n")
    assert report["level"] == "ERROR"
    assert report["libraries"] == dict.fromkeys(LIBRARIES, "ERROR")
    assert "probe: a warning line" not in log
    assert "probe: an httpx warning" not in log
