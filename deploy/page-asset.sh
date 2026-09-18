#!/bin/bash
# Does the page render, or merely answer? The single implementation of that
# assertion. Sourced by staging-smoke-test.sh and by CI's production smoke tests.
#
# A status check cannot see the failure this exists for. A bundle built for one
# mount point and served at another returns its index.html intact, with a 200 and
# every header in place, while each asset URL inside it points somewhere that
# does not hold the file — a blank screen behind a green tick.
#
# The content type carries the assertion, not the status code. An SPA fallback
# answers a missed asset with index.html and a 200, so a code-only check passes
# on exactly the case this is written to catch; what proves the asset resolved is
# JavaScript coming back, and the browser applies the same test before it will
# execute a module script.

# assert_page_asset <page-url>
#
# 0 = the first script the page names resolves to JavaScript. 1 = it does not, or
# the page names none. Prints the diagnosis; the caller owns the PASS/FAIL
# reporting, as with assert_origin_marker in deploy/origin-marker.sh.
assert_page_asset() {
    local page="$1" curl_opts src origin url result code ctype
    curl_opts="-s --connect-timeout 10 --max-time 30"

    # shellcheck disable=SC2086 # deliberate word splitting of the option list
    src=$(curl $curl_opts "$page" 2>/dev/null | grep -o 'src="[^"]*\.js"' | sed 's/^src="//; s/"$//' | head -n1)
    if [ -z "$src" ]; then
        echo "${page} names no script, so either it did not answer with the bundle's"
        echo "index.html or the build stopped emitting one."
        return 1
    fi

    # Resolved as a browser would against the page's own URL. Vite writes these
    # absolutely from its `base`, so in practice the second arm is the live one;
    # the third covers a relative-base build, where the prefix comes from the
    # page's directory instead.
    origin="${page%%://*}://$(echo "${page#*://}" | cut -d/ -f1)"
    case "$src" in
        http*) url="$src" ;;
        /*)    url="${origin}${src}" ;;
        *)     url="${page%/*}/${src}" ;;
    esac

    # shellcheck disable=SC2086
    result=$(curl $curl_opts -o /dev/null -w '%{http_code} %{content_type}' "$url" 2>/dev/null) || {
        echo "unreachable: ${url}"
        return 1
    }
    code="${result%% *}"
    ctype="${result#* }"
    case "${code}:${ctype}" in
        200:*javascript*|200:*ecmascript*) return 0 ;;
    esac

    echo "${page} asks for ${src}, which answered ${code} ${ctype} at ${url}."
    if [ "$code" = "200" ]; then
        echo "A 200 that is not JavaScript is the SPA fallback: the asset missed and"
        echo "index.html was served in its place, so the page loads and renders nothing."
    fi
    echo "The bundle's base path and the mount it is served at disagree."
    return 1
}
