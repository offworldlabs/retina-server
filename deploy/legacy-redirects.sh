#!/bin/bash
# Do the retired hostnames still reach the surface they were retired into? The
# single implementation of that assertion. Sourced by staging-smoke-test.sh and
# by CI's production smoke tests.
#
# `map`, `dash` and `data` no longer have a vhost. Nothing in this
# repo answers on them: they are Cloudflare redirect rules onto the equivalent
# page on their environment's `app` host, and if a rule is removed or misedited
# the name reaches the origin, where the catch-all refuses it with a 421. No
# other check here would notice, because every check here is written against the
# names that survived.
#
# The app host does not translate old addresses, so each rule must land on the
# final one itself. `dash` carries the path, because the console's pages kept
# their names at the root; `map` and `data` were one page each, and land on
# /map and /data.
#
# Reported as a warning by both callers, never as a failure. These rules live at
# the edge rather than in this repo, so a deploy cannot have broken one and
# rolling a release back cannot fix one — the same reasoning that makes a
# missing DNS record non-blocking in the staging suite.

# assert_legacy_redirect <retired-host> <path> <expected-target>
#
# e.g. assert_legacy_redirect dash.retina.fm /leaderboard https://app.retina.fm/leaderboard
#      assert_legacy_redirect map.retina.fm / https://app.retina.fm/map
#
# 0 = a 301 to the expected place. 1 = anything else. Prints the diagnosis; the
# caller owns the reporting, as with assert_page_asset in deploy/page-asset.sh.
assert_legacy_redirect() {
    local host="$1" path="$2" target="$3" curl_opts probe result code loc want
    curl_opts="-s --connect-timeout 10 --max-time 30"

    # A query string on every probe, so this also asserts the rule carries it.
    # A static target with `preserve_query_string` off drops it, which for a
    # bookmarked link is a silent landing on the wrong view rather than an
    # error. The fragment, where the map keeps its view, never reaches the edge:
    # the browser carries it across a Location that has none.
    probe="${path}?keep=1"
    want="${target}?keep=1"

    # shellcheck disable=SC2086 # deliberate word splitting of the option list
    result=$(curl $curl_opts -o /dev/null -w '%{http_code} %{redirect_url}' \
        "https://${host}${probe}" 2>/dev/null) || {
        echo "unreachable: https://${host}${probe}"
        return 1
    }
    code="${result%% *}"
    loc="${result#* }"

    if [ "$code" = "301" ] && [ "$loc" = "$want" ]; then
        return 0
    fi
    echo "https://${host}${probe} answered ${code} → ${loc:-no Location}, wanted 301 → ${want}."
    if [ "$code" = "421" ]; then
        echo "A 421 is our own catch-all: the request reached the origin, so the edge"
        echo "is no longer redirecting this name at all."
    fi
    return 1
}
