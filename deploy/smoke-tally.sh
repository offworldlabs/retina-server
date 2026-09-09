#!/bin/bash
# The pass/fail/warn bookkeeping both smoke suites share, on the same footing as
# tower-contract.sh: sourced by deploy/staging-smoke-test.sh and by the
# production smoke step in .github/workflows/ci.yml, so the two cannot report
# the same outcome differently.
#
# Only the counters and the summary live here. check_status and the rest are
# still defined per suite and have drifted (production retries once on a
# connection error, staging does not); unifying those changes what fails a
# deploy, so it is not a refactor. ClickUp 123zgec1jxv.

PASS=0
FAIL=0
# A warned check did not run. Tallied apart from the passes rather than folded
# into them, so a substitution is visible in the line people actually read.
WARN=0

# One format, no arguments: a caller that could vary it could reintroduce the
# divergence this file exists to prevent.
smoke_summary() {
    if [ "$WARN" -gt 0 ]; then
        echo "Results: ${PASS} passed, ${FAIL} failed, ${WARN} warned"
    else
        echo "Results: ${PASS} passed, ${FAIL} failed"
    fi
}
