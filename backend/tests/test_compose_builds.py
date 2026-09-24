"""A build that changes nothing keeps its image ID, so a deploy leaves the running
container alone. docker-compose.yml says why provenance is the setting that decides it.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def test_every_image_the_base_builds_has_provenance_disabled():
    services = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]
    provenance = {name: service["build"].get("provenance") for name, service in services.items() if "build" in service}
    assert provenance == {"server": "disabled=true", "fleet": "disabled=true"}


def test_no_overlay_touches_a_build():
    # How an image is built is not per-environment, so by the base file's own
    # rule it lives there alone, and an overlay's provenance would override it.
    for overlay in sorted(REPO.glob("docker-compose.*.yml")):
        assert not re.findall(r"^\s*(?:build|provenance):", overlay.read_text(), re.M), overlay.name
