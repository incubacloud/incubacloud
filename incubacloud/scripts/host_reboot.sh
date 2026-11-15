#!/usr/bin/env bash
# Restart the server for its updates, when the panel says so.
#
# Usage:
#   host_reboot.sh check
#   host_reboot.sh guard
#   host_reboot.sh reboot
#
# The server no longer restarts by itself: unattended-upgrades installs
# its patches with ``Automatic-Reboot "false"``, and the panel restarts
# the server with a job of its own, once nothing of its runs there and
# inside the window the host is given (``cloud.host``).
#
# Orders:
#   check   Says whether the server waits for a restart, and why. Prints
#             IC_BOOT_ID=<id>            /proc/sys/kernel/random/boot_id
#             IC_REBOOT_REQUIRED=<0|1>   /run/reboot-required is there
#             IC_REBOOT_PKGS=<pkg ...>   /run/reboot-required.pkgs, one line
#             IC_POLICY=<fixed|kept|none>
#           and puts ``Automatic-Reboot "false"`` in the policy hardening
#           wrote, if it still says "true" (``fixed``; ``kept`` when it
#           said "false" already, ``none`` with no such file): the servers
#           hardened before the panel restarted them stop restarting on
#           their own, with no new hardening run.
#   guard   Exit 75 with ``IC_VETO=<reason>`` when the server is busy with
#           something the panel cannot see from its jobs: a step of a job
#           run apart (``detached_step.sh``) still alive, a copy or a load
#           (duplicity, pg_dump, pg_restore, pg_basebackup, an image
#           build), or an executable of /etc/incubacloud/reboot-guard.d
#           that exits non-zero (its owner's own veto). Exit 0 otherwise.
#   reboot  ``shutdown -r +1``, then checks that systemd has the restart
#           scheduled (/run/systemd/shutdown/scheduled). Prints
#           ``IC_SCHEDULED=1``. From here no new SSH session opens until
#           the server is back: logind writes /run/nologin at once.
#
# Every path is the system's own; the tests point them elsewhere through
# IC_RUN_DIR, IC_BOOT_ID_FILE, IC_UNATTENDED_FILE, IC_GUARD_DIR and
# IC_DETACHED_GLOB.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "host_reboot.sh <check|guard|reboot>"

RUN_DIR="${IC_RUN_DIR:-/run}"
BOOT_ID_FILE="${IC_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
UNATTENDED_FILE="${IC_UNATTENDED_FILE:-/etc/apt/apt.conf.d/52incubacloud-unattended}"
GUARD_DIR="${IC_GUARD_DIR:-/etc/incubacloud/reboot-guard.d}"
DETACHED_GLOB="${IC_DETACHED_GLOB:-/tmp/.incubacloud-detached-*}"
#: Seconds an executable of the guard directory may take.
GUARD_TIMEOUT_S=60
EXIT_VETO=75

# Run "$@" as root: directly when we are, else through sudo, which must not
# ask for a password.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo -n "$@"
    fi
}

boot_id() {
    tr -d '[:space:]' <"$BOOT_ID_FILE"
}

do_check() {
    local required=0 pkgs="" policy=none
    if [ -e "${RUN_DIR}/reboot-required" ]; then
        required=1
        if [ -r "${RUN_DIR}/reboot-required.pkgs" ]; then
            pkgs="$(sort -u "${RUN_DIR}/reboot-required.pkgs" | tr '\n' ' ')"
            pkgs="${pkgs% }"
        fi
    fi
    if [ -f "$UNATTENDED_FILE" ]; then
        if grep -q 'Automatic-Reboot "true"' "$UNATTENDED_FILE"; then
            as_root sed -i \
                -e 's/Automatic-Reboot "true"/Automatic-Reboot "false"/' \
                -e '/Automatic-Reboot-Time/d' \
                "$UNATTENDED_FILE"
            policy=fixed
        else
            policy=kept
        fi
    fi
    printf 'IC_BOOT_ID=%s\n' "$(boot_id)"
    printf 'IC_REBOOT_REQUIRED=%s\n' "$required"
    printf 'IC_REBOOT_PKGS=%s\n' "$pkgs"
    printf 'IC_POLICY=%s\n' "$policy"
}

# Whether the step run apart in directory $1 still runs: it has not written
# its exit status, and the process its ``pid`` file names is that process
# (same start time, not a zombie), or something of its session is alive.
detached_alive() {
    local dir="$1" pid sid start stat rest state now
    local -a fields
    [ -r "${dir}/pid" ] || return 1
    [ ! -e "${dir}/exit" ] || return 1
    read -r pid sid start <"${dir}/pid" || return 1
    if stat="$(cat "/proc/${pid}/stat" 2>/dev/null)"; then
        rest="${stat##*) }"
        read -r -a fields <<<"$rest"
        state="${fields[0]}"
        now="${fields[19]}"
        if [ "$now" = "$start" ] && [ "$state" != Z ]; then
            return 0
        fi
    fi
    [ -n "$sid" ] && pgrep -s "$sid" >/dev/null 2>&1
}

veto() {
    printf 'IC_VETO=%s\n' "$1"
    exit "$EXIT_VETO"
}

do_guard() {
    local dir name pattern guard
    for dir in $DETACHED_GLOB; do
        [ -d "$dir" ] || continue
        if detached_alive "$dir"; then
            veto "a step of a job still runs apart on the server ($(basename -- "$dir"))"
        fi
    done
    for name in duplicity pg_dump pg_restore pg_basebackup; do
        if pgrep -x "$name" >/dev/null 2>&1; then
            veto "${name} is running"
        fi
    done
    for pattern in '^docker build( |$)' '^docker[ -]compose .*build( |$)' 'docker-buildx'; do
        if pgrep -f -- "$pattern" >/dev/null 2>&1; then
            veto "an image build is running"
        fi
    done
    if [ -d "$GUARD_DIR" ]; then
        for guard in "$GUARD_DIR"/*; do
            if [ ! -f "$guard" ] || [ ! -x "$guard" ]; then continue; fi
            if ! timeout "$GUARD_TIMEOUT_S" "$guard" >/dev/null 2>&1; then
                veto "$(basename -- "$guard") in ${GUARD_DIR} said no"
            fi
        done
    fi
    echo "IC_VETO="
}

do_reboot() {
    as_root shutdown -r +1
    if [ ! -e "${RUN_DIR}/systemd/shutdown/scheduled" ]; then
        ic_die "shutdown returned, but no restart is scheduled"
    fi
    echo "IC_SCHEDULED=1"
}

case "$1" in
    check) do_check ;;
    guard) do_guard ;;
    reboot) do_reboot ;;
    *) ic_die "unknown order: $1" ;;
esac
