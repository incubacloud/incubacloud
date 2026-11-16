#!/usr/bin/env bash
# Restart the server for its updates, when the panel says so.
#
# Usage:
#   host_reboot.sh check [--auto <0|1>] [--window <HH:MM> <hours> <tz>]
#                        [--away <seconds>] [--guard]
#                        [--inside <dir> <pg_user> <dbname> <sql>]...
#                        [--expect [container...]]
#   host_reboot.sh guard
#   host_reboot.sh prepare
#   host_reboot.sh reboot
#   host_reboot.sh cancel
#   host_reboot.sh verify [container...]
#   host_reboot.sh away
#
# The server no longer restarts by itself while the panel looks after it:
# unattended-upgrades installs its patches with ``Automatic-Reboot
# "false"``, and the panel restarts the server with a job of its own, once
# nothing of its runs there and inside the window the host is given
# (``cloud.host``). A server the panel has not checked for a day restarts
# by itself again (``away``), inside the same window and with the same
# veto.
#
# Orders:
#   check   Says whether the server waits for a restart, and why. Prints
#             IC_BOOT_ID=<id>            /proc/sys/kernel/random/boot_id
#             IC_REBOOT_REQUIRED=<0|1>   /run/reboot-required is there
#             IC_REBOOT_PKGS=<pkg ...>   /run/reboot-required.pkgs, one line
#             IC_POLICY=<fixed|kept|none>
#             IC_AWAY_RESTART=<epoch>    when ``away`` restarted it, if
#                                        since the panel's last check
#           and puts ``Automatic-Reboot "false"`` in the policy hardening
#           wrote, if it still says "true" (``fixed``; ``kept`` when it
#           said "false" already, ``none`` with no such file): the servers
#           hardened before the panel restarted them stop restarting on
#           their own, with no new hardening run. Where needrestart is
#           installed, keeps it off cloud-init's hotplug hook with the
#           drop-in the Full Setup writes (``ic_needrestart_keep_hotplug``):
#           the servers set up before it get it with no new Full Setup; one
#           that cannot be written is warned of and fails nothing.
#           With ``--auto``, ``--window`` and ``--away``, the panel's mark
#           (``${STATE_DIR}/panel-seen``: when it checked, whether it
#           restarts the server by itself, its window, how long it may be
#           away) and the instances to count inside (``--inside``, kept in
#           ``${STATE_DIR}/inside``) are written for ``away``, and with
#           ``--auto 1`` its timer is installed. Not written for a host the
#           panel does not restart by itself, unless a mark is there
#           already: it is then written saying so.
#           With ``--guard`` and a restart pending, prints
#           ``IC_VETO=<reason>``, empty when the server would say yes: the
#           veto of ``guard``, and what runs inside each ``--inside``.
#           With ``--expect``, prints ``IC_DOCKER=<active|inactive>`` and
#           ``IC_MISSING=<name>`` for each container named that does not
#           run, at once.
#   guard   Exit 75 with ``IC_VETO=<reason>`` when the server is busy with
#           something the panel cannot see from its jobs: a step of a job
#           run apart (``detached_step.sh``) still alive, a copy or a load
#           (duplicity, pg_dump, pg_restore, pg_basebackup, an image
#           build), or an executable of /etc/incubacloud/reboot-guard.d
#           that exits non-zero (its owner's own veto). Exit 0 otherwise.
#   prepare Prints ``IC_CONTAINER=<name>`` for each container running now
#           whose restart policy brings it back after the boot (``always``,
#           ``unless-stopped``), which the panel expects back, and
#           ``IC_NOT_EXPECTED=<name>`` for each other one: started without
#           a policy, or ``on-failure``, which Docker does not start again
#           after a clean stop. Runs a ``CHECKPOINT`` in each PostgreSQL of
#           a stack (the compose service ``db``), so that its stop at the
#           shutdown writes little: best effort, a database that does not
#           answer is left as it is.
#   reboot  ``shutdown -r +1``, then checks that systemd has the restart
#           scheduled (/run/systemd/shutdown/scheduled). Prints
#           ``IC_SCHEDULED=1``. From here no new SSH session opens until
#           the server is back: logind writes /run/nologin at once.
#   cancel  ``shutdown -c``, then checks that nothing is scheduled. Prints
#           ``IC_CANCELLED=1``; fails when the restart is still scheduled.
#   verify  After the boot: prints IC_BOOT_ID, IC_REBOOT_REQUIRED and
#           ``IC_DOCKER=<active|inactive>``; waits up to IC_VERIFY_WAIT_S
#           (300) for each container named to run again, and prints
#           ``IC_MISSING=<name>`` for each one that does not; prints
#           ``IC_UNCLEAN=<name>`` for each PostgreSQL whose start in this
#           boot says it was not shut down clean (its log's last ``database
#           system was`` line since the boot: ``was shut down at`` is
#           clean; the lines of the stop itself never reach the log, Docker
#           is gone by then).
#   away    Run by the timer ``incubacloud-reboot-when-away`` every 30
#           minutes, as root. Restarts the server when all of this holds:
#           the panel's mark says it restarts the server by itself; the
#           panel has not checked it for longer than the mark's ``away``
#           (a panel that sleeps, or is gone); a restart is pending; the
#           window of the mark is open; the server has not restarted this
#           way in this opening of the window; ``guard`` says yes; no SSH
#           session is open (the panel's jobs all come through one);
#           nothing runs inside the instances of the mark. Checkpoints the
#           databases as ``prepare`` does and gives ``shutdown -r +1``.
#           Exit 0 whatever it decided; it says why in the journal.
#
# Every path is the system's own; the tests point them elsewhere through
# IC_RUN_DIR, IC_BOOT_ID_FILE, IC_UNATTENDED_FILE, IC_GUARD_DIR,
# IC_DETACHED_GLOB, IC_STATE_DIR, IC_LIB_DIR, IC_SYSTEMD_DIR and
# IC_PROC_STAT, and the clock through IC_NOW.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

ic_require_args 1 "$#" "host_reboot.sh <check|guard|prepare|reboot|cancel|verify|away>"

RUN_DIR="${IC_RUN_DIR:-/run}"
BOOT_ID_FILE="${IC_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
UNATTENDED_FILE="${IC_UNATTENDED_FILE:-/etc/apt/apt.conf.d/52incubacloud-unattended}"
GUARD_DIR="${IC_GUARD_DIR:-/etc/incubacloud/reboot-guard.d}"
DETACHED_GLOB="${IC_DETACHED_GLOB:-/tmp/.incubacloud-detached-*}"
STATE_DIR="${IC_STATE_DIR:-/var/lib/incubacloud/reboot}"
LIB_DIR="${IC_LIB_DIR:-/usr/local/lib/incubacloud}"
SYSTEMD_DIR="${IC_SYSTEMD_DIR:-/etc/systemd/system}"
PROC_STAT="${IC_PROC_STAT:-/proc/stat}"
VERIFY_WAIT_S="${IC_VERIFY_WAIT_S:-300}"
VERIFY_POLL_S="${IC_VERIFY_POLL_S:-10}"
#: Seconds a CHECKPOINT may take before the restart goes on without it.
CHECKPOINT_TIMEOUT_S=120
#: Seconds an executable of the guard directory may take.
GUARD_TIMEOUT_S=60
EXIT_VETO=75
#: The timer and the service of ``away``.
AWAY_UNIT=incubacloud-reboot-when-away
#: An SSH session open on the server, as sshd titles it: ``sshd:
#: user@notty``, or ``sshd-session: user@notty`` from OpenSSH 9.8.
SSH_SESSION_RE='^sshd(-session)?: [^ ]+@'

#: The instances whose jobs inside are counted: <dir> <pg_user> <dbname>
#: <sql>, four words per instance.
INSIDE=()

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

now_s() {
    printf '%s\n' "${IC_NOW:-$(date +%s)}"
}

# Write stdin to file $1 as root, in one move.
write_root() {
    as_root tee "$1.tmp" >/dev/null
    as_root mv -f "$1.tmp" "$1"
}

# Write the panel's mark and the instances to count inside.
# $1 auto, $2 window start (HH:MM), $3 hours, $4 time zone, $5 away (s).
write_mark() {
    local index=0 dir
    as_root mkdir -p "$STATE_DIR"
    printf 'seen=%s\nauto=%s\nstart=%s\nhours=%s\ntz=%s\naway=%s\n' \
        "$(now_s)" "$1" "$2" "$3" "$4" "$5" | write_root "${STATE_DIR}/panel-seen"
    as_root rm -rf "${STATE_DIR}/inside"
    as_root mkdir -p "${STATE_DIR}/inside"
    while [ "$index" -lt "${#INSIDE[@]}" ]; do
        # Absolute: ``away`` runs as root, whose home is not the
        # operator's.
        dir="$(ic_expand_home "${INSIDE[$index]}")"
        printf '%s\0' "$dir" "${INSIDE[$((index + 1))]}" \
            "${INSIDE[$((index + 2))]}" "${INSIDE[$((index + 3))]}" \
            | write_root "${STATE_DIR}/inside/$((index / 4))"
        index=$((index + 4))
    done
}

# Install a copy of this script and of its library, and the timer of
# ``away``. Rewritten only when they changed.
install_away() {
    local here changed=0 unit
    here="$(dirname "${BASH_SOURCE[0]}")"
    if ! cmp -s "${BASH_SOURCE[0]}" "${LIB_DIR}/host_reboot.sh" \
            || ! cmp -s "${here}/lib/common.sh" "${LIB_DIR}/lib/common.sh"; then
        as_root install -D -m 0755 "${BASH_SOURCE[0]}" "${LIB_DIR}/host_reboot.sh"
        as_root install -D -m 0644 "${here}/lib/common.sh" "${LIB_DIR}/lib/common.sh"
    fi
    unit="$(cat <<EOF
[Unit]
Description=IncubaCloud: restart for updates while the panel is away

[Service]
Type=oneshot
ExecStart=/bin/bash ${LIB_DIR}/host_reboot.sh away
EOF
)"
    if [ "$(cat "${SYSTEMD_DIR}/${AWAY_UNIT}.service" 2>/dev/null)" != "$unit" ]; then
        printf '%s\n' "$unit" | write_root "${SYSTEMD_DIR}/${AWAY_UNIT}.service"
        changed=1
    fi
    unit="$(cat <<EOF
[Unit]
Description=IncubaCloud: restart for updates while the panel is away

[Timer]
OnCalendar=*:0/30
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF
)"
    if [ "$(cat "${SYSTEMD_DIR}/${AWAY_UNIT}.timer" 2>/dev/null)" != "$unit" ]; then
        printf '%s\n' "$unit" | write_root "${SYSTEMD_DIR}/${AWAY_UNIT}.timer"
        changed=1
    fi
    if [ "$changed" -eq 1 ]; then
        as_root systemctl daemon-reload
        as_root systemctl enable --now "${AWAY_UNIT}.timer" >/dev/null 2>&1
    fi
}

do_check() {
    local required=0 pkgs="" policy=none auto="" start="" hours="" tz="" away=""
    local guard=0 expect=0 reason name seen last
    local -a expected=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --auto) auto="$2"; shift 2 ;;
            --window) start="$2"; hours="$3"; tz="$4"; shift 4 ;;
            --away) away="$2"; shift 2 ;;
            --guard) guard=1; shift ;;
            --inside) INSIDE+=("$2" "$3" "$4" "$5"); shift 5 ;;
            --expect) expect=1; shift; expected=("$@"); break ;;
            *) ic_die "unknown option of check: $1" ;;
        esac
    done
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
    ic_needrestart_keep_hotplug || :
    # The panel's last check, before this one writes its own.
    seen="$(sed -n 's/^seen=//p' "${STATE_DIR}/panel-seen" 2>/dev/null)" || seen=""
    [[ "$seen" =~ ^[0-9]+$ ]] || seen=0
    if [ -n "$auto" ] && [ -n "$start" ] && [ -n "$away" ]; then
        if [ "$auto" = 1 ] || [ -e "${STATE_DIR}/panel-seen" ]; then
            write_mark "$auto" "$start" "$hours" "$tz" "$away"
        fi
        if [ "$auto" = 1 ]; then
            install_away
        fi
    fi
    printf 'IC_BOOT_ID=%s\n' "$(boot_id)"
    printf 'IC_REBOOT_REQUIRED=%s\n' "$required"
    printf 'IC_REBOOT_PKGS=%s\n' "$pkgs"
    printf 'IC_POLICY=%s\n' "$policy"
    if [ -r "${STATE_DIR}/away-restart" ]; then
        last="$(tr -d '[:space:]' <"${STATE_DIR}/away-restart")"
        if [[ "$last" =~ ^[0-9]+$ ]] && [ "$last" -gt "$seen" ]; then
            printf 'IC_AWAY_RESTART=%s\n' "$last"
        fi
    fi
    if [ "$guard" -eq 1 ] && [ "$required" -eq 1 ]; then
        reason=""
        if reason="$(local_veto)"; then :; elif reason="$(inside_veto)"; then :; else reason=""; fi
        printf 'IC_VETO=%s\n' "$reason"
    fi
    if [ "$expect" -eq 1 ]; then
        if systemctl is-active --quiet docker; then
            echo "IC_DOCKER=active"
        else
            echo "IC_DOCKER=inactive"
        fi
        for name in "${expected[@]}"; do
            container_running "$name" || printf 'IC_MISSING=%s\n' "$name"
        done
    fi
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

# Print why the server says no and return 0, or return 1 when it says yes:
# what ``guard`` reads.
local_veto() {
    local dir name pattern guard
    for dir in $DETACHED_GLOB; do
        [ -d "$dir" ] || continue
        if detached_alive "$dir"; then
            echo "a step of a job still runs apart on the server ($(basename -- "$dir"))"
            return 0
        fi
    done
    for name in duplicity pg_dump pg_restore pg_basebackup; do
        if pgrep -x "$name" >/dev/null 2>&1; then
            echo "${name} is running"
            return 0
        fi
    done
    for pattern in '^docker build( |$)' '^docker[ -]compose .*build( |$)' 'docker-buildx'; do
        if pgrep -f -- "$pattern" >/dev/null 2>&1; then
            echo "an image build is running"
            return 0
        fi
    done
    if [ -d "$GUARD_DIR" ]; then
        for guard in "$GUARD_DIR"/*; do
            if [ ! -f "$guard" ] || [ ! -x "$guard" ]; then continue; fi
            if ! timeout "$GUARD_TIMEOUT_S" "$guard" >/dev/null 2>&1; then
                echo "$(basename -- "$guard") in ${GUARD_DIR} said no"
                return 0
            fi
        done
    fi
    return 1
}

# Print what runs inside an instance of INSIDE and return 0, or return 1
# when nothing does. The count of ``move_steps.sh quiet``: a stack that is
# parked counts 0; one that cannot be counted says so.
inside_veto() {
    local index=0 dir count
    while [ "$index" -lt "${#INSIDE[@]}" ]; do
        dir="$(ic_expand_home "${INSIDE[$index]}")"
        count="$(count_inside "$dir" "${INSIDE[$((index + 1))]}" \
            "${INSIDE[$((index + 2))]}" "${INSIDE[$((index + 3))]}" 2>/dev/null)" || count=""
        if ! [[ "$count" =~ ^[0-9]+$ ]]; then
            echo "what runs inside $(basename -- "$dir") could not be counted"
            return 0
        fi
        if [ "$count" -gt 0 ]; then
            echo "${count} job(s) run inside $(basename -- "$dir")"
            return 0
        fi
        index=$((index + 4))
    done
    return 1
}

# Print the count *sql* gives in the database of the stack in $1.
# $1 dir, $2 pg_user, $3 dbname, $4 sql.
count_inside() {
    local running
    cd "$1" || return 1
    running="$(ic_running_services)" || return 1
    if ! grep -qx db <<<"$running"; then
        echo 0
        return 0
    fi
    printf '%s\n' "$4" \
        | docker compose exec -T db psql -U "$2" -d "$3" -tA -v ON_ERROR_STOP=1 \
        | tr -d '\r ' | awk 'NF' | tail -n 1
}

do_guard() {
    local reason
    if reason="$(local_veto)"; then
        veto "$reason"
    fi
    echo "IC_VETO="
}

# The PostgreSQL containers of the stacks running now.
db_containers() {
    docker ps --filter label=com.docker.compose.service=db --format '{{.Names}}'
}

checkpoint_dbs() {
    local name
    db_containers | while read -r name; do
        [ -n "$name" ] || continue
        # shellcheck disable=SC2016  # expanded inside the container
        timeout "$CHECKPOINT_TIMEOUT_S" docker exec "$name" sh -c \
            'psql -U "${POSTGRES_USER:-postgres}" -d postgres -tAc CHECKPOINT' \
            >/dev/null 2>&1 || ic_log "CHECKPOINT did not answer in ${name}"
    done
}

do_prepare() {
    local name policy
    docker ps --format '{{.Names}}' | sort | while read -r name; do
        [ -n "$name" ] || continue
        policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name" 2>/dev/null)" \
            || policy=""
        case "$policy" in
            always|unless-stopped) printf 'IC_CONTAINER=%s\n' "$name" ;;
            *) printf 'IC_NOT_EXPECTED=%s\n' "$name" ;;
        esac
    done
    checkpoint_dbs
}

# Whether container $1 runs.
container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]
}

do_verify() {
    local name waited=0 missing line since
    local -a pending=("$@") since_arg=()
    printf 'IC_BOOT_ID=%s\n' "$(boot_id)"
    if [ -e "${RUN_DIR}/reboot-required" ]; then
        echo "IC_REBOOT_REQUIRED=1"
    else
        echo "IC_REBOOT_REQUIRED=0"
    fi
    if systemctl is-active --quiet docker; then
        echo "IC_DOCKER=active"
    else
        echo "IC_DOCKER=inactive"
    fi
    while :; do
        missing=()
        for name in "${pending[@]}"; do
            container_running "$name" || missing+=("$name")
        done
        pending=("${missing[@]}")
        if [ "${#pending[@]}" -eq 0 ] || [ "$waited" -ge "$VERIFY_WAIT_S" ]; then
            break
        fi
        sleep "$VERIFY_POLL_S"
        waited=$((waited + VERIFY_POLL_S))
    done
    for name in "${pending[@]}"; do
        printf 'IC_MISSING=%s\n' "$name"
    done
    # Only this boot's lines: a PostgreSQL that has not written its own yet
    # is not read from the start before.
    since="$(awk '$1 == "btime" { print $2 }' "$PROC_STAT" 2>/dev/null)" || since=""
    if [ -n "$since" ]; then since_arg=(--since "$since"); fi
    db_containers | while read -r name; do
        [ -n "$name" ] || continue
        line="$(docker logs "${since_arg[@]}" --tail 500 "$name" 2>&1 \
            | grep 'database system was' | tail -n 1)" || true
        if [ -n "$line" ] && [[ "$line" != *"was shut down at"* ]]; then
            printf 'IC_UNCLEAN=%s\n' "$name"
        fi
    done
}

do_reboot() {
    as_root shutdown -r +1
    if [ ! -e "${RUN_DIR}/systemd/shutdown/scheduled" ]; then
        ic_die "shutdown returned, but no restart is scheduled"
    fi
    echo "IC_SCHEDULED=1"
}

do_cancel() {
    as_root shutdown -c || true
    if [ -e "${RUN_DIR}/systemd/shutdown/scheduled" ]; then
        ic_die "the restart is still scheduled"
    fi
    echo "IC_CANCELLED=1"
}

# Whether the window from $1 (HH:MM) for $2 hours, in zone $3, is open at
# $4 (epoch). Sets OPENED_AT to when this opening began.
window_open() {
    local start="$1" hours="$2" tz="$3" now="$4" hm now_min start_min elapsed
    [[ "$start" =~ ^[0-9]{2}:[0-9]{2}$ && "$hours" =~ ^[0-9]+$ ]] || return 1
    hm="$(TZ="$tz" date -d "@${now}" +%H:%M)" || return 1
    now_min=$((10#${hm%:*} * 60 + 10#${hm#*:}))
    start_min=$((10#${start%:*} * 60 + 10#${start#*:}))
    elapsed=$(((now_min - start_min + 1440) % 1440))
    [ "$elapsed" -lt $((hours * 60)) ] || return 1
    OPENED_AT=$((now - elapsed * 60 - now % 60))
}

do_away() {
    local mark="${STATE_DIR}/panel-seen" key value seen="" auto="" start="" hours=""
    local tz="" away="" now last=0 reason spec
    local -a fields
    if [ ! -r "$mark" ]; then
        ic_log "the panel never checked this server: nothing to do"
        return 0
    fi
    while IFS='=' read -r key value; do
        case "$key" in
            seen) seen="$value" ;;
            auto) auto="$value" ;;
            start) start="$value" ;;
            hours) hours="$value" ;;
            tz) tz="$value" ;;
            away) away="$value" ;;
        esac
    done <"$mark"
    if [ "$auto" != 1 ]; then
        ic_log "the panel does not restart this server by itself: nothing to do"
        return 0
    fi
    if ! [[ "$seen" =~ ^[0-9]+$ && "$away" =~ ^[0-9]+$ ]]; then
        ic_log "the panel's mark cannot be read: nothing to do"
        return 0
    fi
    now="$(now_s)"
    if [ $((now - seen)) -lt "$away" ]; then
        ic_log "the panel checked this server $(((now - seen) / 60)) min ago: it decides"
        return 0
    fi
    if [ ! -e "${RUN_DIR}/reboot-required" ]; then
        ic_log "no restart pending"
        return 0
    fi
    if ! window_open "$start" "$hours" "$tz" "$now"; then
        ic_log "outside the window (from ${start} for ${hours} h, ${tz})"
        return 0
    fi
    if [ -r "${STATE_DIR}/away-restart" ]; then
        last="$(tr -d '[:space:]' <"${STATE_DIR}/away-restart")"
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    if [ "$last" -ge "$OPENED_AT" ]; then
        ic_log "restarted once in this window already"
        return 0
    fi
    if reason="$(local_veto)"; then
        ic_log "not restarted: ${reason}"
        return 0
    fi
    if pgrep -f -- "$SSH_SESSION_RE" >/dev/null 2>&1; then
        ic_log "not restarted: an SSH session is open"
        return 0
    fi
    for spec in "${STATE_DIR}"/inside/*; do
        [ -f "$spec" ] || continue
        mapfile -d '' -t fields <"$spec"
        [ "${#fields[@]}" -eq 4 ] || continue
        INSIDE+=("${fields[@]}")
    done
    if reason="$(inside_veto)"; then
        ic_log "not restarted: ${reason}"
        return 0
    fi
    checkpoint_dbs
    printf '%s\n' "$now" | write_root "${STATE_DIR}/away-restart"
    ic_log "restarting in a minute: the panel has not checked this server for $(((now - seen) / 3600)) h"
    as_root shutdown -r +1
}

case "$1" in
    check) shift; do_check "$@" ;;
    guard) do_guard ;;
    prepare) do_prepare ;;
    reboot) do_reboot ;;
    cancel) do_cancel ;;
    verify) shift; do_verify "$@" ;;
    away) do_away ;;
    *) ic_die "unknown order: $1" ;;
esac
