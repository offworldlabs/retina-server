#!/bin/bash
# Which origin answered, and the smoke suites' implementation of that assertion.
# Sourced by staging-smoke-test.sh and by CI's production smoke tests;
# deploy/verify-deploy.py reads the two values below and checks them in Python.
#
# A probe proves a hostname responded, not that THIS service responded. Both
# origins behind retina.fm answer /api/health with 200 and the same CSP, HSTS,
# X-Frame-Options and X-Content-Type-Options, so until 2026-09-16 nothing in a
# response told them apart. When an Origin Rule moved towers.retina.fm to
# tower-finder-service on 2026-09-14, every probe kept passing against a
# different service and the smoke gate noticed nothing.
#
# The value is sent by deploy/nginx/snippets/origin-marker.conf. The two are
# pinned together by backend/tests/test_nginx_origin_marker.py, because they are
# in different languages in different directories and drift between them turns
# every probe below into an assertion about nothing.
#
# Both lowercase, and they must stay that way: the assertion downcases the whole
# header block before matching, because HTTP/2 lowercases field names and
# HTTP/1.1 does not. An uppercase letter in either would match nothing at
# runtime while every test still passed, so the tests require lowercase too.
RETINA_ORIGIN_HEADER="x-retina-origin"
RETINA_ORIGIN_VALUE="retina-server"

# assert_origin_marker <url>
#
# 0 = this origin answered. 1 = something else did, or nothing did.
# Prints the diagnosis; the caller owns the PASS/FAIL reporting, as with
# assert_tower_contract in deploy/tower-contract.sh.
#
# Probe an /api/ path, not `/` or a static file. nginx drops every inherited
# add_header in a location that declares its own, and snippets/spa.conf declares
# three — so /index.html and everything under /assets/ carry no marker by
# design, and asserting there would fail against a correctly configured origin.
assert_origin_marker() {
    local url="$1" headers found attempt
    # Two attempts, as assert_tower_contract does. The production suite runs
    # immediately after the app restarts and rolls production back on failure,
    # so a warm-up blip must not read as a repointed hostname.
    for attempt in 1 2; do
        headers=$(curl -s --connect-timeout 10 --max-time 30 -o /dev/null -D - "$url" 2>/dev/null) && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        echo "unreachable after 2 attempts: ${url}"
        return 1
    done

    found=$(printf '%s' "$headers" | tr 'A-Z' 'a-z' | grep "^${RETINA_ORIGIN_HEADER}:" | head -n1)
    if [ -z "$found" ]; then
        echo "${url} carries no ${RETINA_ORIGIN_HEADER} header, so this repo's nginx did not"
        echo "answer it. Either an Origin Rule now points the hostname at another service,"
        echo "or the vhost lost its security-headers include."
        return 1
    fi

    if ! printf '%s' "$found" | grep -qF "$RETINA_ORIGIN_VALUE"; then
        echo "${url} answered with ${found}, expected ${RETINA_ORIGIN_VALUE}."
        return 1
    fi
    return 0
}
