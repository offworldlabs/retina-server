"""The files that let a phone open the RETINA app from a sign-in link.

iOS reads /.well-known/apple-app-site-association and Android reads
/.well-known/assetlinks.json on the app host. Each is built from one setting,
so a deployment that has not set it answers 404 and the link opens the page.
Neither is API, so neither is in the OpenAPI document.
"""

import logging
import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

APP_ID = "retina.fm"

_TEAM_ID = re.compile(r"[A-Z0-9]{10}")
_FINGERPRINT = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}")


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association():
    team = os.getenv("MOBILE_APPLE_TEAM_ID", "").strip()
    if not team:
        raise HTTPException(status_code=404)
    if not _TEAM_ID.fullmatch(team):
        logger.warning("MOBILE_APPLE_TEAM_ID is not a 10-character Apple team ID; serving no association")
        raise HTTPException(status_code=404)
    return JSONResponse(
        {"applinks": {"details": [{"appIDs": [f"{team}.{APP_ID}"], "components": [{"/": "/auth/link/*"}]}]}}
    )


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
async def asset_links():
    fingerprints = []
    for entry in os.getenv("MOBILE_ANDROID_CERT_SHA256", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if _FINGERPRINT.fullmatch(entry):
            fingerprints.append(entry.upper())
        else:
            logger.warning("MOBILE_ANDROID_CERT_SHA256 holds an entry that is not a SHA-256 fingerprint; skipped")
    if not fingerprints:
        raise HTTPException(status_code=404)
    return JSONResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": APP_ID,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        ]
    )
