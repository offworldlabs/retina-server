#!/bin/bash
# The droplet deploy, one copy for all three environments. deploy/gate.sh runs
# it from the commit being deployed, with a clean environment holding only:
#
#   DEPLOY_ENV     prod | staging | test, from the box's own hostname
#   APP_DIR        the deploy directory
#   RUN_ID         the Actions run it is for, written into the marker
#   TARGET_SHA     the commit to deploy, already fetched
#   DEPLOY_BRANCH  main, or on test the branch TARGET_SHA came from
#   FAIL_AT        test only: none | preflight | after-marker | after-build,
#                  failures injected to exercise the rollback
#
# Every check that can refuse sits above the rollback point and touches
# nothing. Below it the box is mid-deploy, and .deploy-in-progress says so,
# until the new container answers /api/health. The gate's rollback verb keys on
# that marker, and on the .last-deploy record written once the deploy is done.
set -euo pipefail

CF_CA=/etc/ssl/cloudflare/origin-pull-ca.pem
CARTO_ENV=/root/.secrets/carto.env

main() {
    : "${DEPLOY_ENV:?}" "${APP_DIR:?}" "${RUN_ID:?}" "${TARGET_SHA:?}"
    DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
    FAIL_AT="${FAIL_AT:-none}"
    case "$DEPLOY_ENV" in
        prod) LABEL=production ;;
        staging) LABEL=staging ;;
        test) LABEL="test" ;;
        *) fail "unknown DEPLOY_ENV '${DEPLOY_ENV}'." ;;
    esac
    [ "$DEPLOY_ENV" = test ] || [ "$FAIL_AT" = none ] || fail "failures are injected on the test droplet only."
    cd "$APP_DIR"

    preflight
    inject preflight "Nothing on this box has been touched. Expect the rollback to find no marker and decline."

    # The record describes a deploy whose rollback point is about to be
    # retaken, so it can no longer be rolled back to.
    rm -f .last-deploy
    # The rollback point: the running image retagged :rollback and the current
    # commit tagged deploy-<timestamp>. No rollback point, no deploy.
    bash deploy/pre-deploy.sh
    echo "run ${RUN_ID} started $(date -u +%Y-%m-%dT%H:%M:%SZ)" >.deploy-in-progress
    inject after-marker "Expect the rollback to act and the marker to be cleared."

    git reset --hard "$TARGET_SHA"
    git submodule update --init --recursive
    # This environment's overlay, written from the tree every deploy so that a
    # rebuilt box or a hand-edited .env cannot select another.
    cp "deploy/env.${DEPLOY_ENV}.example" .env
    # This host's CARTO basemap key; see deploy/env.<env>.example. A host with
    # none builds watermarked tiles rather than failing its deploy.
    if [ -f "$CARTO_ENV" ]; then cat "$CARTO_ENV" >>.env; fi

    # Dangling images only: `-a` would take the :rollback image, which no
    # container references. Before the builder prune, which cannot free a layer
    # an image still holds. The cache keeps 10GB so builds stay incremental.
    docker image prune -f
    docker builder prune -f --keep-storage 10GB
    if [ "$DEPLOY_ENV" = test ]; then
        # The shared nginx template proxies /api/towers over retina-edge, which
        # compose declares external, so `up` fails outright without it.
        docker network create retina-edge >/dev/null 2>&1 || true
    fi
    # `server` alone: no environment deploys the fleet, and naming it would
    # enable its `sim` profile. No `down` first: `up --build` swaps the
    # container only once the image exists, so a failed build leaves the old
    # one serving. --remove-orphans clears a container a renamed service left
    # holding the pinned name the new one needs.
    docker compose up -d --build --remove-orphans server
    inject after-build "Expect the rollback to restore the previous image and commit."

    # The tree's own copy, now the deployed commit's.
    bash deploy/wait-for-health.sh || exit 1
    # The record before the marker goes, so a deploy that dies between the two
    # is still marked, and rolled back as one that failed after it began.
    printf 'run_id=%s\nsha=%s\nfinished=%s\n' "$RUN_ID" "$TARGET_SHA" "$(date +%s)" >.last-deploy
    rm -f .deploy-in-progress
    echo "Deployed ${TARGET_SHA} to ${LABEL}."

    # Housekeeping after a healthy deploy only warns: failing here would read as
    # a failed deploy and roll back a good one.
    if [ "$DEPLOY_ENV" = staging ]; then
        # A profile-gated service is invisible to --remove-orphans, so a fleet
        # container left on staging, which runs no simulator, is removed here.
        docker rm -f retina-staging-fleet >/dev/null 2>&1 || true
    fi
    # The gate follows main, never a branch deployed to test.
    if [ "$DEPLOY_BRANCH" = main ]; then
        bash deploy/gate.sh --install ||
            echo "::warning::The deploy succeeded but the gate was not refreshed from ${TARGET_SHA}. Run 'bash deploy/gate.sh --install' on $(hostname)."
    fi
}

fail() {
    echo "::error::$*"
    exit 1
}

# Refusals only, every one of them before the rollback point.
preflight() {
    # Identity first: the one check that does not depend on the state of the
    # box. Exact, since `latest` and `contest` both contain `test`.
    [ "$(hostname)" = "retina-${DEPLOY_ENV}" ] ||
        fail "Refusing to deploy ${LABEL} onto '$(hostname)': expected retina-${DEPLOY_ENV}. If this droplet was rebuilt, finish provisioning it (hostnamectl set-hostname retina-${DEPLOY_ENV})."

    # A marker is a box an earlier deploy left mid-flight. Snapshotting over it
    # would make the broken build the rollback point and lose the good one. Any
    # marker, this run's included: a re-run keeps its run id.
    [ ! -f .deploy-in-progress ] ||
        fail "${APP_DIR}/.deploy-in-progress is still present on $(hostname) ($(cat .deploy-in-progress)). An earlier deploy was left mid-flight and never confirmed healthy. Recover the box (bash deploy/rollback.sh, or fix forward), delete the marker, and deploy again. The running stack is untouched."

    # An .env selecting another overlay means this box is not the environment
    # it is being deployed as, and pre-deploy.sh would snapshot the wrong
    # service through it. A missing .env is a box awaiting its first deploy.
    if [ -f .env ] && ! grep -q "^COMPOSE_FILE=.*docker-compose\.${DEPLOY_ENV}\.yml" .env; then
        fail "${APP_DIR}/.env exists but does not select docker-compose.${DEPLOY_ENV}.yml, so this box is not ${LABEL}. Inspect it; do not deploy."
    fi

    # Each secret check reads the last line, the one compose passes on.
    if [ "$DEPLOY_ENV" = prod ]; then
        # Production refuses to import without it (backend/core/users.py), and
        # 32 characters rejects the placeholder published in backend/.env.example.
        grep -E '^JWT_SECRET=' backend/.env 2>/dev/null | tail -1 | grep -qE '^JWT_SECRET=.{32,}$' ||
            fail "backend/.env has no effective JWT_SECRET of at least 32 characters, and production refuses to boot without one. On the droplet, replacing any existing line: printf 'JWT_SECRET=%s\n' \"\$(openssl rand -hex 32)\" >> ${APP_DIR}/backend/.env"
    fi
    if [ "$DEPLOY_ENV" != test ]; then
        # The test router's reads admit the smoke suite by this key and fail
        # closed without one, and production refuses to import without it
        # (backend/routes/test.py).
        local key_secret=STAGING_RADAR_API_KEY without="the smoke suite cannot read the test router"
        if [ "$DEPLOY_ENV" = prod ]; then
            key_secret=RADAR_API_KEY without="production refuses to boot"
        fi
        grep -E '^RADAR_API_KEY=' backend/.env 2>/dev/null | tail -1 | grep -qE '^RADAR_API_KEY=.+$' ||
            fail "backend/.env on $(hostname) has no effective RADAR_API_KEY, and without one ${without}. Set it to the ${key_secret} secret's value and deploy again. The running stack is untouched."

        # nginx verifies Cloudflare's client certificate and will not start
        # without the CA, which is per-box state that no deploy ships.
        [ -f "$CF_CA" ] ||
            fail "${CF_CA} is missing on $(hostname). nginx requires Cloudflare's origin-pull CA and will not start without it. Fetch it (curl -fsS --create-dirs -o ${CF_CA} https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem) and deploy again. The running stack is untouched."
        openssl x509 -in "$CF_CA" -noout >/dev/null 2>&1 ||
            fail "${CF_CA} on $(hostname) is not a parseable PEM certificate, so nginx would not start. Replace it and deploy again. The running stack is untouched."
    fi

    # On a full disk the build can reuse every cached layer and pass on
    # yesterday's code.
    local avail_mb
    avail_mb=$(df -m / | awk 'NR==2 {print $4}')
    if [ "${avail_mb:-0}" -lt 2048 ]; then
        df -h / || true
        docker system df 2>/dev/null || true
        fail "${LABEL} disk has only ${avail_mb}MB free (under 2GB). Refusing to deploy."
    fi

    if [ "$DEPLOY_ENV" = test ]; then
        local members
        members=$(docker network inspect retina-edge --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || true)
        if [ -z "${members// /}" ]; then
            echo "::warning::Nothing is on retina-edge on $(hostname), so /api/towers will answer 502 here after this deploy. Deploy tower-finder-service to this droplet to fix it."
        fi
    fi
}

inject() {
    [ "$FAIL_AT" = "$1" ] || return 0
    fail "Injected failure at ${1} (fail_at=${1}). $2"
}

main "$@"
exit
