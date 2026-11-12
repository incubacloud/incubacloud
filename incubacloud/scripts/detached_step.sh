#!/usr/bin/env bash
# Run one long step of a job on its server, apart from the SSH session.
#
# Usage:
#   detached_step.sh start <dir> <cap_s> <scope> <job> <label> <command>
#   <dir>/ctl.sh read <dir> <offset> [max_bytes]
#   <dir>/ctl.sh result <dir>
#   <dir>/ctl.sh stop <dir> <wait_s>
#   <dir>/ctl.sh clean <dir>
#
# A step that runs inside the panel's SSH session dies with the request
# that drives it, and on a host where Odoo cuts a request at
# ``limit_time_real`` (900 s on Odoo.sh) a build or a restore outlives it.
# Started here, the step runs in a session of its own (``setsid -f nohup``)
# and leaves in <dir> what the panel needs to follow it from any later
# request: its output, its exit status, whether its cap cut it.
#
# The script needs nothing else on the server: ``start`` copies it into
# <dir> as ``ctl.sh``, and every other order is run from that copy, so
# following or stopping a step needs the directory's name only. The panel
# names it from the job and the step (``detached_dir``); nobody else on
# the server can tell the name in advance.
#
# Files in <dir>, created 0700 by ``start`` (``mkdir -m 700``, no ``-p``):
#   scope     ``instance:<id>`` or ``host``: what one step at a time holds
#   job       the job's id and the step's label
#   deadline  when the cap ends, in epoch seconds
#   cmd       the command, run with ``bash -c`` from $HOME
#   pid       "<pid> <session> <starttime>", written by the step's process
#             as it starts; the start time from /proc tells a reused pid
#   log       stdout and stderr, each in its order (two pipes: a line of
#             one may come before a line of the other written just ahead)
#   out       stdout only, for the step's result
#   exit      the exit status, moved into place as the process's last act
#   timedout  there when the cap cut the step (``timeout`` exits 124, or
#             137, past the deadline)
#   stopped   there when ``stop`` signalled it
#
# Orders:
#   start   Under a lock of this account. The step's command runs with
#           the umask of who starts it, as it would attached; the files
#           of <dir> are created under 077. A step whose ``pid`` is there
#           already started (or ended): nothing is done, exit 0. Exit 73
#           when a step of another directory holds the same scope and
#           still runs (it says of which job), 74 when something that is
#           not this account's directory is at <dir>, 78 when the server
#           kills this account's processes once its SSH session ends
#           (logind's KillUserProcesses): the step would die with it, and
#           the panel runs it attached instead; 79, run attached too, when
#           the server lacks a tool the orders need (``REQUIRED_TOOLS``).
#   read    One header line, then the bytes [offset, off) of ``log``:
#             ic-detached v1 state=<running|done|lost|none> exit=<n|-> \
#               timedout=<0|1> off=<n>
#           While the step runs, the bytes end at the last newline (the
#           rest comes with the next read); at most max_bytes (1 MiB) at a
#           time. ``done`` and ``lost`` are said only once the bytes reach
#           the end of ``log``: until then the state reads ``running``.
#           ``lost``: there is a ``pid``, no ``exit``, and no such process.
#           ``none``: no directory, or no ``pid`` in it.
#   result  The last 256 KiB of ``out``.
#   stop    TERM to the step's whole session, a wait of up to <wait_s>,
#           then KILL to what is left; marks ``stopped``. A session whose
#           leader's pid names another process now is not the step's. Exit 0 with
#           nothing alive, 1 if something outlived the KILL. What runs in
#           a container is not in the session: the step's own trap, which
#           receives the TERM, removes it, as when a job is stopped today.
#   clean   Removes <dir> once nothing of the step is alive; exit 75 while
#           something is, 2 for a path not named .incubacloud-detached-<hex>.
#
# ``start``, ``stop`` and ``clean`` can be repeated; ``read`` and
# ``result`` change nothing.
set -eu
# The caller's, for the step's command (``_run``): a step apart creates
# files as the same command attached would. Read before the 077 below.
CALLER_UMASK="$(umask)"
umask 077

#: Header of ``read``: the version tells a panel what it parses.
HEADER="ic-detached v1"
#: A step's cap is enforced by ``timeout``; what ignores its TERM is killed
#: this many seconds later (the wait the panel's transport gives a stopped
#: command's trap, ``CANCEL_TERM_WAIT_S``).
KILL_AFTER_S=60
#: Default and upper bound of what one ``read`` returns, in bytes
#: (``DETACHED_READ_MAX_BYTES``).
READ_MAX_BYTES=1048576
#: What ``result`` returns of ``out``, in bytes (``DETACHED_STDOUT_TAIL``).
RESULT_TAIL_BYTES=262144
#: The name every step directory starts with, for the scope check.
DIR_PREFIX=".incubacloud-detached-"
#: What the orders run besides coreutils: without one of them a step could
#: start and never be followed or stopped, so ``start`` refuses (79).
REQUIRED_TOOLS="flock setsid timeout pgrep pkill"
#: The ``timeout`` that runs a step. The uutils one of Ubuntu 26.04 answers a
#: TERM from outside with 124 whatever the child did, and the status of a
#: stopped step's trap would be lost; the GNU one, kept there as
#: ``gnutimeout``, passes the child's status on.
TIMEOUT="$(command -v gnutimeout || printf timeout)"

say() {
    printf '[incubacloud] %s\n' "$*"
}

die() {
    printf '[incubacloud] ERROR: %s\n' "$*"
    exit "${2:-1}"
}

usage() {
    die "usage: detached_step.sh start <dir> <cap_s> <scope> <job> <label> <command> | read <dir> <offset> [max_bytes] | result <dir> | stop <dir> <wait_s> | clean <dir>" 2
}

is_count() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

# Whether $1 is a directory of this account, and not a link to one.
ours() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ -O "$1" ]
}

# Print the state (field 3 of /proc/<pid>/stat) and the start time (field
# 22) of process $1. The command name, field 2, may hold spaces and
# parentheses: the fields are counted after its closing one.
proc_stat() {
    local stat rest fields
    stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    rest="${stat##*) }"
    read -r -a fields <<<"$rest"
    printf '%s %s\n' "${fields[0]}" "${fields[19]}"
}

# Print the start time of process $1.
starttime() {
    local state start
    read -r state start < <(proc_stat "$1") || return 1
    printf '%s\n' "$start"
}

# Whether process $1 runs: it exists and is not a zombie. Where nothing
# reaps orphans (a container whose first process does not), a step's
# processes stay zombies once they end.
running() {
    local state start
    read -r state start < <(proc_stat "$1") || return 1
    [ "$state" != Z ]
}

# Whether the process that ran the step in $1 is still that process.
leader_alive() {
    local pid sid start state now
    [ -r "$1/pid" ] || return 1
    read -r pid sid start <"$1/pid" || return 1
    read -r state now < <(proc_stat "$pid") || return 1
    [ "$state" != Z ] && [ "$now" = "$start" ]
}

# Print the session of the step in $1, from its ``pid`` file.
session_of() {
    local pid sid start
    [ -r "$1/pid" ] || return 1
    read -r pid sid start <"$1/pid" || return 1
    printf '%s\n' "$sid"
}

# Whether any process of this account still runs in session $1.
session_alive() {
    local pid
    for pid in $(pgrep -s "$1" -U "$(id -u)" 2>/dev/null); do
        running "$pid" && return 0
    done
    return 1
}

# Whether something of the step in $1 still runs. Its session is the one
# its leader opened, named by the leader's pid: while that pid is another
# process (a different start time), the session it names is not the
# step's, and nothing of the step is alive. With the leader gone, what is
# left in the session is the step's: no pid is given again while a session
# still bears it.
step_alive() {
    local pid sid start state now
    [ -r "$1/pid" ] || return 1
    read -r pid sid start <"$1/pid" || return 1
    if read -r state now < <(proc_stat "$pid"); then
        [ "$now" = "$start" ] || return 1
    fi
    session_alive "$sid"
}

# Whether $1 is named as a step's directory, the only kind ``clean``
# removes.
step_dir_name() {
    [[ "$(basename -- "$1")" =~ ^\.incubacloud-detached-[0-9a-f]+$ ]]
}

# Take this account's lock of the step directories, on descriptor 9.
# In $HOME: a lock in a directory every account writes in could be taken,
# or replaced, by another one.
take_lock() {
    [ -n "${HOME:-}" ] || die "HOME is not set: there is no place for the lock of the detached steps"
    exec 9>>"$HOME/.incubacloud-detached.lock"
    flock -w 60 9 || die "could not take the lock of the detached steps"
}

# Whether logind kills this account's processes when its session ends.
#
# ``KillUserProcesses=yes`` (the default outside Debian and Ubuntu) kills
# a session's whole scope at logout, ``setsid`` included; ``linger`` does
# not spare that scope. A user in ``KillExcludeUsers`` (root when the list
# is empty), or outside a non-empty ``KillOnlyUsers``, is spared. Without
# logind there is nothing to ask, and nothing that kills.
kills_user_processes() {
    local kill excl only user
    command -v busctl >/dev/null 2>&1 || return 1
    kill="$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
        org.freedesktop.login1.Manager KillUserProcesses 2>/dev/null)" || return 1
    [ "$kill" = "b true" ] || return 1
    user="$(id -un)"
    excl="$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
        org.freedesktop.login1.Manager KillExcludeUsers 2>/dev/null)" || excl="as 0"
    only="$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
        org.freedesktop.login1.Manager KillOnlyUsers 2>/dev/null)" || only="as 0"
    if [ "$excl" = "as 0" ]; then
        [ "$user" = root ] && return 1
    elif [[ " $excl " == *" \"$user\" "* ]]; then
        return 1
    fi
    if [ "$only" != "as 0" ] && [[ " $only " != *" \"$user\" "* ]]; then
        return 1
    fi
    return 0
}

# Print the ``job`` of a step that holds scope $2 and still runs, other
# than the one in $1; fail when there is none.
live_scope_holder() {
    local dir="$1" scope="$2" other
    for other in "$(dirname "$dir")/$DIR_PREFIX"*; do
        [ "$other" = "$dir" ] && continue
        ours "$other" || continue
        [ -e "$other/exit" ] && continue
        [ "$(cat "$other/scope" 2>/dev/null)" = "$scope" ] || continue
        leader_alive "$other" || continue
        cat "$other/job" 2>/dev/null || printf 'unknown\n'
        return 0
    done
    return 1
}

# Print the tools of ``REQUIRED_TOOLS`` this server lacks; fail when it has
# them all.
missing_tools() {
    local tool missing=""
    for tool in $REQUIRED_TOOLS; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    [ -n "$missing" ] || return 1
    printf '%s\n' "${missing# }"
}

cmd_start() {
    [ "$#" -eq 6 ] || usage
    local dir="$1" cap="$2" scope="$3" job="$4" label="$5" command="$6"
    local holder missing
    if ! is_count "$cap" || [ "$cap" -eq 0 ]; then
        die "the cap must be a number of seconds: $cap" 2
    fi
    if missing="$(missing_tools)"; then
        die "this server lacks $missing: the step cannot run apart" 79
    fi
    take_lock
    if ours "$dir" && [ -e "$dir/pid" ]; then
        exit 0
    fi
    if { [ -e "$dir" ] || [ -L "$dir" ]; } && ! ours "$dir"; then
        die "$dir is taken by something that is not this account's directory" 74
    fi
    if kills_user_processes; then
        die "this server kills $(id -un)'s processes when its SSH session ends (logind KillUserProcesses): the step cannot run apart" 78
    fi
    if holder="$(live_scope_holder "$dir" "$scope")"; then
        die "another step still runs for $scope on this server: job $holder" 73
    fi
    if ! mkdir -m 700 "$dir" 2>/dev/null; then
        ours "$dir" || die "$dir is taken by something that is not this account's directory" 74
        chmod 700 "$dir"
    fi
    printf '%s\n' "$scope" >"$dir/scope"
    printf '%s %s\n' "$job" "$label" >"$dir/job"
    printf '%s\n' "$(($(date +%s) + cap))" >"$dir/deadline"
    printf '%s\n' "$command" >"$dir/cmd"
    if [ "$(readlink -f "${BASH_SOURCE[0]}")" != "$(readlink -f "$dir/ctl.sh")" ]; then
        cp -- "${BASH_SOURCE[0]}" "$dir/ctl.sh"
    fi
    # Descriptor 9 holds the lock: the step must not keep it.
    setsid -f nohup bash "$dir/ctl.sh" _run "$dir" "$cap" "$CALLER_UMASK" \
        </dev/null >/dev/null 2>&1 9>&-
    # Held until the step wrote its ``pid``: a second ``start`` then finds
    # it and does nothing.
    local _
    for _ in $(seq 1 100); do
        [ -e "$dir/pid" ] && exit 0
        sleep 0.1
    done
    # Still under the lock: a step that starts after this finds no
    # directory to write its ``pid`` in, and ends before its command.
    rm -rf -- "$dir"
    die "the step did not start within 10 s: its directory was removed"
}

# The step's own process: run the command under its cap, with umask $3
# (022 when it is not one), then record how it ended. Not an order for the
# panel.
cmd_run() {
    local dir="$1" cap="$2" mask="${3:-}" rc deadline
    [[ "$mask" =~ ^[0-7]{3,4}$ ]] || mask=022
    cd "${HOME:-/}" 2>/dev/null || cd /
    # Created before the command's umask is set: they stay 0600.
    : >>"$dir/log"
    : >>"$dir/out"
    printf '%s %s %s\n' "$$" "$$" "$(starttime "$$")" >"$dir/pid.tmp"
    mv -f "$dir/pid.tmp" "$dir/pid"
    # A handler, not an ignore: ignored signals are inherited, handled ones
    # are not. A ``stop`` reaches the command, and this shell lives on to
    # record how it ended.
    trap ':' TERM
    {
        trap ':' TERM
        rc=0
        # In this subshell only: what the step records below stays 077.
        umask "$mask"
        "$TIMEOUT" --kill-after="$KILL_AFTER_S" "$cap" \
            bash -c "$(cat "$dir/cmd")" 2>>"$dir/log" || rc=$?
        umask 077
        printf '%s\n' "$rc" >"$dir/rc"
    } </dev/null | (trap '' TERM; exec tee -a "$dir/out") >>"$dir/log"
    rc="$(cat "$dir/rc" 2>/dev/null || printf '143')"
    rm -f "$dir/rc"
    deadline="$(cat "$dir/deadline")"
    # 124 may be the command's own ``timeout``, 137 a KILL of the step's
    # own: only past the deadline are they the cap's.
    if { [ "$rc" = 124 ] || { [ "$rc" = 137 ] && [ ! -e "$dir/stopped" ]; }; } \
        && [ "$(date +%s)" -ge "$deadline" ]; then
        : >"$dir/timedout"
    fi
    printf '%s\n' "$rc" >"$dir/exit.tmp"
    mv -f "$dir/exit.tmp" "$dir/exit"
}

# Print the bytes [$2, $2 + $3) of file $1.
slice() {
    dd if="$1" bs=65536 iflag=skip_bytes,count_bytes skip="$2" count="$3" \
        status=none
}

# Print how many of the $3 bytes from offset $2 of file $1 end at a newline.
upto_newline() {
    local file="$1" off="$2" len="$3" last partial
    [ "$len" -gt 0 ] || { printf '0\n'; return; }
    last="$(slice "$file" "$off" "$len" | tail -c 1 | od -An -tx1 | tr -d ' \n')"
    if [ "$last" = 0a ]; then
        printf '%s\n' "$len"
        return
    fi
    partial="$(slice "$file" "$off" "$len" | LC_ALL=C tail -n 1 | wc -c)"
    printf '%s\n' "$((len - partial))"
}

cmd_read() {
    if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
        usage
    fi
    local dir="$1" off="$2" max="${3:-$READ_MAX_BYTES}"
    local state exit_status="-" timedout=0 size len cut
    is_count "$off" || die "the offset must be a number of bytes: $off" 2
    if ! is_count "$max" || [ "$max" -eq 0 ]; then
        die "max_bytes must be a positive number: $max" 2
    fi
    if ! ours "$dir" || [ ! -e "$dir/pid" ]; then
        printf '%s state=none exit=- timedout=0 off=%s\n' "$HEADER" "$off"
        return 0
    fi
    # ``exit`` before and after the liveness check: the step may end
    # between the two.
    if [ -e "$dir/exit" ]; then
        state="done"
    elif leader_alive "$dir"; then
        state=running
    elif [ -e "$dir/exit" ]; then
        state="done"
    else
        state=lost
    fi
    size="$(stat -c %s "$dir/log" 2>/dev/null || printf '0')"
    [ "$off" -le "$size" ] || off="$size"
    len=$((size - off))
    if [ "$len" -gt "$max" ]; then
        len="$max"
        state=running
    fi
    if [ "$state" = running ]; then
        cut="$(upto_newline "$dir/log" "$off" "$len")"
        # A single line longer than a whole read goes as it is, or the
        # reads would stall on it.
        if [ "$cut" -gt 0 ] || [ "$len" -lt "$max" ]; then
            len="$cut"
        fi
    elif [ "$state" = "done" ]; then
        exit_status="$(cat "$dir/exit")"
        [ -e "$dir/timedout" ] && timedout=1
    fi
    printf '%s state=%s exit=%s timedout=%s off=%s\n' \
        "$HEADER" "$state" "$exit_status" "$timedout" "$((off + len))"
    [ "$len" -gt 0 ] && slice "$dir/log" "$off" "$len"
    return 0
}

cmd_result() {
    [ "$#" -eq 1 ] || usage
    local dir="$1"
    ours "$dir" && [ -e "$dir/out" ] || return 0
    tail -c "$RESULT_TAIL_BYTES" "$dir/out"
}

cmd_stop() {
    [ "$#" -eq 2 ] || usage
    local dir="$1" wait_s="$2" sid uid tries
    is_count "$wait_s" || die "the wait must be a number of seconds: $wait_s" 2
    ours "$dir" || exit 0
    sid="$(session_of "$dir")" || exit 0
    step_alive "$dir" || exit 0
    uid="$(id -u)"
    : >"$dir/stopped"
    say "stopping the step: TERM, then KILL after ${wait_s}s"
    pkill -TERM -s "$sid" -U "$uid" 2>/dev/null || true
    for tries in $(seq 1 $((wait_s * 5))); do
        step_alive "$dir" || exit 0
        sleep 0.2
    done
    pkill -KILL -s "$sid" -U "$uid" 2>/dev/null || true
    for tries in $(seq 1 25); do
        step_alive "$dir" || exit 0
        sleep 0.2
    done
    : "$tries"
    die "something of the step outlived its KILL"
}

cmd_clean() {
    [ "$#" -eq 1 ] || usage
    local dir="$1"
    step_dir_name "$dir" || die "$dir is not named as a step's directory: not removed" 2
    take_lock
    [ -e "$dir" ] || [ -L "$dir" ] || exit 0
    ours "$dir" || die "$dir is not this account's directory: not removed" 74
    if step_alive "$dir"; then
        die "the step still runs: $dir was not removed" 75
    fi
    rm -rf -- "$dir"
}

[ "$#" -ge 1 ] || usage
order="$1"
shift
case "$order" in
    start) cmd_start "$@" ;;
    _run) cmd_run "$@" ;;
    read) cmd_read "$@" ;;
    result) cmd_result "$@" ;;
    stop) cmd_stop "$@" ;;
    clean) cmd_clean "$@" ;;
    *) usage ;;
esac
