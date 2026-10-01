#!/bin/bash
# retina-deploy-gate: everything a CI deploy key can do on a droplet.
#
# Installed as /usr/local/sbin/retina-deploy-gate by `bash deploy/gate.sh
# --install`: setup-server.sh installs it and every deploy of main refreshes
# it. A key is authorised for it alone, in root's authorized_keys, as
#
#   restrict,command="/usr/local/sbin/retina-deploy-gate retina-server" ssh-ed25519 AAAA... <comment>
#
# so it cannot open a shell, forward a port or copy a file, whatever the client
# asks for. What the client asked for arrives in SSH_ORIGINAL_COMMAND and must
# be one of:
#
#   deploy <run-id>                      prod, staging: main's tip
#   deploy <run-id> <branch> <fail-at>   test: a branch, and a failure to inject
#   rollback <run-id> deploy|checks      only what that run's deploy left behind
#   probe-towers                         tower-finder-service over retina-edge
#   status                               deployed commit, marker, last deploy
#
# Nothing a verb runs comes from the caller. `deploy` runs deploy/deploy.sh as
# it stands in the commit this box has just fetched from GitHub, and
# `probe-towers` the contract in main's tip, fetched the same way; `rollback`
# runs the tree already checked out, which came from there too. On test the
# caller names the branch, so that key runs, as root there, any branch someone
# with push access has put on GitHub.
set -euo pipefail

# Client-supplied locale variables (sshd accepts LC_*) must not change what the
# character classes below match.
export LC_ALL=C

GATE_PATH=/usr/local/sbin/retina-deploy-gate
# A finished deploy may be rolled back by its own run's checks for this long:
# its smoke and E2E jobs' caps, with margin. After that, or once another deploy
# has replaced it, the run can no longer act on it.
ROLLBACK_WINDOW_S=$((90 * 60))
# Deploys and rollbacks queue here, for no longer than a caller waits: CI's
# jobs allow for it, and past it the caller has gone (see take_lock).
LOCK_WAIT_S=$((15 * 60))

# Kept in variables: bash 3.2 misparses some bracket expressions written inline.
RUN_ID_RE='^[0-9]{1,20}$'
EPOCH_RE='^[0-9]{1,12}$'
BRANCH_RE='^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}$'
REQUEST_RE='^[A-Za-z0-9._/@+ -]*$'

refuse() {
    echo "::error::retina-deploy-gate: $*"
    exit 1
}

# The environment is the box's own. setup-server.sh names it retina-<env>, and
# deploy.sh checks the name again before it touches anything.
set_box_env() {
    case "$(hostname)" in
        retina-prod) BOX_ENV=prod ;;
        retina-staging) BOX_ENV=staging ;;
        retina-test) BOX_ENV="test" ;;
        *) refuse "this box is '$(hostname)', which is no retina environment." ;;
    esac
}

valid_run_id() {
    [[ "$1" =~ $RUN_ID_RE ]]
}

valid_branch() {
    [[ "$1" =~ $BRANCH_RE ]] && git check-ref-format --branch "$1" >/dev/null 2>&1
}

# Git is kept from detaching its automatic maintenance, which would inherit
# the lock below and hold it long after the deploy. For the gate's own git and,
# through run_clean, for everything the deploy and rollback run.
GIT_SETTINGS=(
    GIT_CONFIG_COUNT=2
    GIT_CONFIG_KEY_0=gc.autoDetach GIT_CONFIG_VALUE_0=false
    GIT_CONFIG_KEY_1=maintenance.autoDetach GIT_CONFIG_VALUE_1=false
)
export "${GIT_SETTINGS[@]}"

# Runs a command with a clean environment: the path, home and git settings
# above, and whatever it is told.
run_clean() {
    env -i PATH="$PATH" HOME="${HOME:-/root}" "${GIT_SETTINGS[@]}" "$@"
}

# Everything a deploy or rollback prints goes to the caller and to a log on the
# box, through a relay that outlives the caller. A dropped connection then
# costs the caller its view, never a write that kills the deploy mid-step.
# Called once the lock is held, so no other caller rotates the log under it.
relay_output() {
    local log="${APP_DIR}/.deploy-gate.log"
    # Kept to its last run or so once it passes a megabyte.
    if [ -f "$log" ] && [ "$(wc -c <"$log")" -gt 1048576 ]; then mv -f "$log" "${log}.1"; fi
    exec > >(relay "$log") 2>&1
}
# Never exits early, whatever fails to take a line: every writer upstream of it
# would then die on its next line.
relay() {
    local line
    set +e
    trap '' PIPE
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line" >>"$1" 2>/dev/null
        printf '%s\n' "$line" 2>/dev/null
    done
}

# Every verb works on the deploy directory's clone: what a deploy fetches
# into, what rollback.sh restores and what status reports.
enter_clone() {
    cd "$APP_DIR"
    [ -d .git ] || refuse "${APP_DIR} on $(hostname) is not a git clone, so the gate has nothing to work on. Re-provision it as one (deploy/setup-server.sh)."
}

# Held until this process and every child it started have exited.
take_lock() {
    exec 9>>"${APP_DIR}/.deploy-gate.lock"
    flock -w "$LOCK_WAIT_S" 9 ||
        refuse "another deploy or rollback has held ${APP_DIR} for ${LOCK_WAIT_S}s."
    # Written straight to the caller, before the relay takes over: a caller
    # that gave up while this waited has closed the channel, and the write
    # ends this process before it acts for nobody.
    echo "retina-deploy-gate: ${APP_DIR} is ours."
}

verb_deploy() {
    local run_id="${1:-}" branch=main fail_at=none sha script
    if [ "$BOX_ENV" = test ]; then
        [ $# -eq 3 ] || refuse "usage on test: deploy <run-id> <branch> <fail-at>"
        branch="$2"
        fail_at="$3"
        valid_branch "$branch" || refuse "'${branch}' is not a branch name."
        case "$fail_at" in
            none | preflight | after-marker | after-build) ;;
            *) refuse "fail-at must be none, preflight, after-marker or after-build." ;;
        esac
    else
        [ $# -eq 1 ] || refuse "usage on ${BOX_ENV}: deploy <run-id>. Only main deploys here."
    fi
    valid_run_id "$run_id" || refuse "'${run_id}' is not a run id."

    take_lock
    relay_output
    enter_clone
    # Spelt out in full: a bare name also matches a tag, and a pull request's
    # ref may be a fork's code. Only a branch is ever fetched.
    # --refmap= leaves the remote-tracking refs alone, which probe-towers and a
    # stale origin/<branch> would otherwise contend for.
    git fetch --quiet --refmap= origin "refs/heads/${branch}" ||
        refuse "could not fetch branch '${branch}' from origin. Nothing was touched. Check that it exists, and the network and free disk (df -h /) on $(hostname)."
    sha=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
    git cat-file -e "${sha}:deploy/deploy.sh" 2>/dev/null ||
        refuse "${sha} has no deploy/deploy.sh, so this gate cannot deploy it."

    WORK=$(mktemp -d)
    script="${WORK}/deploy.sh"
    git show "${sha}:deploy/deploy.sh" >"$script"
    echo "retina-deploy-gate: deploying ${branch} at ${sha} to ${BOX_ENV} for run ${run_id}"
    run_clean DEPLOY_ENV="$BOX_ENV" APP_DIR="$APP_DIR" RUN_ID="$run_id" TARGET_SHA="$sha" \
        DEPLOY_BRANCH="$branch" FAIL_AT="$fail_at" \
        bash "$script"
}

# The one place a rollback is decided. The caller says which of its jobs
# failed, and each looks only at what that failure leaves behind.
#
# `rollback <run-id> deploy`, the deploy failed or was cancelled:
#   - a marker naming this run: it failed after it began, so roll back;
#   - no marker, and the last deploy record names this run: the deploy
#     finished all the same, so refuse loudly, since its checks may never have
#     run, and touch nothing, since it may be a healthy earlier attempt's;
#   - no marker otherwise: it failed before touching anything, or a later
#     deploy has replaced it, so there is nothing to undo.
# `rollback <run-id> checks`, its smoke tests or E2E failed:
#   - no marker, and the last deploy record names this run and is still what
#     is checked out: roll back if it finished within the window, and refuse
#     loudly if not;
#   - no marker otherwise: a later deploy has replaced this run's, and its own
#     checks judge it.
# Either way, a marker this run left while rolling back resumes the rollback,
# and any other marker is refused: this run cannot tell a deploy in progress or
# an unrecovered outage from a box already fixed by hand.
verb_rollback() {
    local run_id="${1:-}" failed="${2:-}" marker reason rc
    [ $# -eq 2 ] && { [ "$failed" = deploy ] || [ "$failed" = checks ]; } ||
        refuse "usage: rollback <run-id> deploy|checks"
    valid_run_id "$run_id" || refuse "'${run_id}' is not a run id."

    take_lock
    relay_output
    enter_clone
    # The file is the marker, whatever it holds: deploy.sh refuses on it alone.
    marker=""
    if [ -f .deploy-in-progress ]; then
        marker=$(cat .deploy-in-progress)
        [ -n "${marker//[[:space:]]/}" ] || marker="an empty marker"
    fi
    case "${failed}:${marker}" in
        *":run ${run_id} rolling back "*) reason="resuming the rollback it began (${marker})" ;;
        "deploy:run ${run_id} "*) reason="its deploy failed after it began (${marker})" ;;
        deploy:)
            if record_names "$run_id"; then
                refuse "run ${run_id}'s deploy finished and is what runs, but its caller reports the deploy failed: the caller went away before the deploy ended, or a re-run's deploy refused after an earlier attempt deployed. Its checks may never have run, and nothing was rolled back. Check $(hostname), and run deploy/rollback.sh by hand if it is broken."
            fi
            nothing "its deploy refused before touching anything, or is no longer what runs"
            return 0
            ;;
        checks:)
            if ! record_names "$run_id"; then
                nothing "run ${run_id}'s deploy is not what runs: a later deploy replaced it, it was already rolled back, or the box has been changed since"
                return 0
            fi
            record_is_recent ||
                refuse "run ${run_id}'s deploy is still what runs, but it finished more than $((ROLLBACK_WINDOW_S / 60)) minutes ago, too long for its checks to roll it back. Inspect $(hostname), and run deploy/rollback.sh by hand if it is broken."
            reason="its checks failed after the deploy finished"
            ;;
        *)
            refuse "${APP_DIR}/.deploy-in-progress is not this run's to act on (${marker}). Either a deploy is in progress, or an earlier one never recovered. This run touched nothing; inspect $(hostname) before acting."
            ;;
    esac

    echo "retina-deploy-gate: rolling back ${BOX_ENV} for run ${run_id}: ${reason}"
    # Marked for the whole rollback, so one that is cut off leaves the next
    # deploy refusing rather than snapshotting a half-restored box.
    [ -f .deploy-in-progress ] ||
        echo "run ${run_id} rolling back since $(date -u +%Y-%m-%dT%H:%M:%SZ)" >.deploy-in-progress
    # rollback.sh reports three outcomes, and `set -e` would merge the last two.
    set +e
    run_clean APP_DIR="$APP_DIR" bash deploy/rollback.sh
    rc=$?
    set -e
    case "$rc" in
        0)
            rm -f .deploy-in-progress .last-deploy
            echo "retina-deploy-gate: rollback complete; ${BOX_ENV} is back on the previous build."
            ;;
        2)
            # Serving again, with the database left ahead of the code: no longer
            # mid-deploy, but red, so the report above is read.
            rm -f .deploy-in-progress .last-deploy
            echo "::error::${BOX_ENV} is back up, but a database downgrade is outstanding: see the report above."
            return 2
            ;;
        *)
            # The marker stays: the box is in an unknown state, and the next
            # deploy must refuse rather than snapshot it as a rollback point.
            echo "::error::Rollback FAILED (exit ${rc}). ${BOX_ENV} may be down: recover $(hostname) by hand, then delete ${APP_DIR}/.deploy-in-progress."
            return "$rc"
            ;;
    esac
}

nothing() {
    echo "retina-deploy-gate: nothing to roll back on ${BOX_ENV}: $*."
}

# True if .last-deploy names this run and the tree is still the commit it
# deployed.
record_names() {
    [ -f .last-deploy ] &&
        [ "$(sed -n 's/^run_id=//p' .last-deploy)" = "$1" ] &&
        [ "$(sed -n 's/^sha=//p' .last-deploy)" = "$(git rev-parse HEAD)" ]
}

# True if the last deploy finished within the window, by this box's clock.
record_is_recent() {
    local finished now
    finished=$(sed -n 's/^finished=//p' .last-deploy)
    [[ "$finished" =~ $EPOCH_RE ]] || return 1
    now=$(date +%s)
    [ "$finished" -le "$now" ] && [ $((now - finished)) -le "$ROLLBACK_WINDOW_S" ]
}

# Asks the contract of main's tip, which is what the next deploy ships, not of
# the tree the box is still running. Fetched into a ref of its own, touching
# neither FETCH_HEAD nor anything a deploy reads, so it needs no lock.
verb_probe_towers() {
    local contract ref=refs/retina-deploy-gate/probe-main
    [ $# -eq 0 ] || refuse "usage: probe-towers"
    enter_clone
    git fetch --quiet --no-write-fetch-head --refmap= origin "+refs/heads/main:${ref}" ||
        refuse "could not fetch main from origin to read its contract."
    WORK=$(mktemp -d)
    contract="${WORK}/tower-contract.sh"
    git show "${ref}:deploy/tower-contract.sh" >"$contract"
    # shellcheck source=deploy/tower-contract.sh
    source "$contract"
    assert_tower_contract_over_edge
}

verb_status() {
    [ $# -eq 0 ] || refuse "usage: status"
    enter_clone
    echo "env=${BOX_ENV}"
    echo "head=$(git rev-parse HEAD)"
    if [ -f .deploy-in-progress ]; then
        echo "marker=$(cat .deploy-in-progress)"
    else
        echo "marker="
    fi
    if [ -f .last-deploy ]; then
        sed 's/^/last_deploy_/' .last-deploy
    fi
}

# Installs this file as the gate. Run by root on the box; never reachable over
# SSH, where the forced command always passes the service first.
install_gate() {
    local source="$1" staged
    bash -n "$source"
    # Staged beside the target and renamed over it, so a gate running while it
    # is replaced keeps reading the file it started with.
    staged="$(dirname "$GATE_PATH")/.$(basename "$GATE_PATH").$$"
    install -m 0755 "$source" "$staged" && mv -f "$staged" "$GATE_PATH" ||
        { rm -f "$staged"; refuse "could not install ${GATE_PATH}; the gate already there is unchanged."; }
    echo "Installed ${GATE_PATH}"
}

main() {
    local service="${1:-}" request
    if [ "$service" = --install ]; then
        install_gate "${BASH_SOURCE[0]}"
        return
    fi
    case "$service" in
        retina-server) APP_DIR=/opt/retina-server ;;
        *) refuse "unknown service '${service}'." ;;
    esac
    [ -d "$APP_DIR" ] || refuse "${APP_DIR} does not exist on $(hostname). Provision the box (deploy/setup-server.sh)."
    shift
    # Over SSH the request is SSH_ORIGINAL_COMMAND and the arguments are the
    # forced command's own; by hand, root may pass the verb as arguments.
    if [ "${SSH_ORIGINAL_COMMAND+set}" = set ]; then
        request="$SSH_ORIGINAL_COMMAND"
    else
        request="$*"
    fi
    # Words of these characters only, so nothing below ever sees a quote, a
    # glob, a substitution or a newline.
    [[ "$request" =~ $REQUEST_RE ]] || refuse "the request holds characters no verb takes."
    set_box_env
    set -f
    # shellcheck disable=SC2086  # split on spaces, into words checked above
    set -- $request
    set +f
    WORK=""
    trap '[ -z "$WORK" ] || rm -rf "$WORK"' EXIT
    case "${1:-}" in
        deploy) shift; verb_deploy "$@" ;;
        rollback) shift; verb_rollback "$@" ;;
        probe-towers) shift; verb_probe_towers "$@" ;;
        status) shift; verb_status "$@" ;;
        "") refuse "no verb given. This key runs deploy, rollback, probe-towers or status, and nothing else." ;;
        *) refuse "'${1}' is not a verb. This key runs deploy, rollback, probe-towers or status, and nothing else." ;;
    esac
}

main "$@"
exit
