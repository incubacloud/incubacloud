#!/usr/bin/env bats
# Tests for scripts/detached_step.sh — a job's long step, run apart from the
# SSH session that starts it. Real processes, real signals: only logind
# (``busctl``) is stubbed.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/detached_step.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME" "$TMP/bin"
    D="$TMP/.incubacloud-detached-0123456789abcdef"
    OTHER="$TMP/.incubacloud-detached-fedcba9876543210"
    # A server whose logind lets this account's processes outlive it.
    cat > "$TMP/bin/busctl" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    *KillUserProcesses*) echo "b ${KILL_USER_PROCESSES:-false}" ;;
    *) echo "as 0" ;;
esac
STUB
    chmod +x "$TMP/bin/busctl"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    local dir
    for dir in "$D" "$OTHER"; do
        [ -e "$dir/ctl.sh" ] && bash "$dir/ctl.sh" stop "$dir" 0 >/dev/null 2>&1
    done
    rm -rf "$TMP"
}

# start <dir> <command> [cap] [scope]
start() {
    bash "$SCRIPT" start "$1" "${3:-60}" "${4:-instance:7}" 41 "Build" "$2"
}

# read_step <dir> <offset> [max]: the header line in $header, the bytes in
# $TMP/bytes. From the copy in <dir>, as the panel reads, once there is one.
read_step() {
    local ctl="$1/ctl.sh"
    [ -e "$ctl" ] || ctl="$SCRIPT"
    bash "$ctl" read "$1" "$2" ${3:+"$3"} > "$TMP/read"
    header="$(head -n 1 "$TMP/read")"
    tail -c +$(( $(head -n 1 "$TMP/read" | wc -c) + 1 )) "$TMP/read" > "$TMP/bytes"
}

# wait_state <dir> <state>: read from 0 until the header says <state>.
wait_state() {
    local _
    for _ in $(seq 1 100); do
        read_step "$1" 0
        [[ "$header" == *"state=$2 "* ]] && return 0
        sleep 0.1
    done
    echo "last header: $header" >&2
    return 1
}

@test "a step runs apart and its output, status and stdout are kept" {
    run start "$D" 'echo one; echo err >&2; echo two; exit 3'
    [ "$status" -eq 0 ]
    [ "$(stat -c %a "$D")" = 700 ]
    wait_state "$D" done
    [[ "$header" == "ic-detached v1 state=done exit=3 timedout=0 off="* ]]
    # Two pipes: stderr's line may come anywhere between stdout's.
    [ "$(grep -v err "$TMP/bytes")" = "$(printf 'one\ntwo')" ]
    grep -qx err "$TMP/bytes"
    run bash "$D/ctl.sh" result "$D"
    [ "$output" = "$(printf 'one\ntwo')" ]
    [ "$(cat "$D/scope")" = "instance:7" ]
    [ "$(cat "$D/job")" = "41 Build" ]
}

@test "the step runs from the account's home, with no input" {
    start "$D" 'pwd; cat; echo end'
    wait_state "$D" done
    [ "$(cat "$TMP/bytes")" = "$(printf '%s\nend' "$HOME")" ]
}

@test "the step creates files with the umask of who starts it, as attached" {
    (umask 022 && start "$D" 'umask; echo a > "$HOME/file"; mkdir "$HOME/dir"')
    wait_state "$D" done
    [ "$(cat "$TMP/bytes")" = "0022" ]
    [ "$(stat -c %a "$HOME/file")" = 644 ]
    [ "$(stat -c %a "$HOME/dir")" = 755 ]
    # The step's own directory and files stay its account's only.
    [ "$(stat -c %a "$D")" = 700 ]
    [ "$(stat -c %a "$D/log")" = 600 ]
    [ "$(stat -c %a "$D/out")" = 600 ]
    [ "$(stat -c %a "$D/exit")" = 600 ]
}

@test "a stricter umask of who starts it is kept, not widened to 022" {
    (umask 077 && start "$D" 'echo a > "$HOME/file"; mkdir "$HOME/dir"')
    wait_state "$D" done
    [ "$(stat -c %a "$HOME/file")" = 600 ]
    [ "$(stat -c %a "$HOME/dir")" = 700 ]
}

@test "a umask _run cannot read falls back to 022" {
    mkdir -m 700 "$D"
    cp "$SCRIPT" "$D/ctl.sh"
    printf '%s\n' "$(($(date +%s) + 60))" > "$D/deadline"
    printf '%s\n' 'umask; echo a > "$HOME/file"' > "$D/cmd"
    (umask 077 && bash "$D/ctl.sh" _run "$D" 60 'u=rwx' </dev/null >/dev/null 2>&1)
    [ "$(cat "$D/exit")" = 0 ]
    [ "$(cat "$D/out")" = "0022" ]
    [ "$(stat -c %a "$HOME/file")" = 644 ]
}

@test "a second start does nothing: the step runs once" {
    start "$D" 'echo ran >> "$HOME/runs"; sleep 1'
    run start "$D" 'echo ran >> "$HOME/runs"; sleep 1'
    [ "$status" -eq 0 ]
    wait_state "$D" done
    run start "$D" 'echo ran >> "$HOME/runs"'
    [ "$status" -eq 0 ]
    [ "$(wc -l < "$HOME/runs")" -eq 1 ]
}

@test "two starts at once run the step once" {
    start "$D" 'echo ran >> "$HOME/runs"; sleep 1' &
    start "$D" 'echo ran >> "$HOME/runs"; sleep 1' &
    wait
    wait_state "$D" done
    [ "$(wc -l < "$HOME/runs")" -eq 1 ]
}

@test "a read while the step runs stops at the last newline" {
    start "$D" 'printf "first\nsec"; sleep 3; printf "ond\nthird\n"'
    for _ in $(seq 1 25); do
        read_step "$D" 0
        [[ "$header" == *"off=0" ]] || break
        sleep 0.1
    done
    [[ "$header" == "ic-detached v1 state=running exit=- timedout=0 off=6" ]]
    [ "$(cat "$TMP/bytes")" = "first" ]
    wait_state "$D" done
    read_step "$D" 6
    [[ "$header" == *"state=done exit=0 timedout=0 off=19" ]]
    [ "$(cat "$TMP/bytes")" = "$(printf 'second\nthird')" ]
}

@test "reads in pieces deliver every byte once, not UTF-8 included" {
    start "$D" 'printf "a\xff\xfeb\n\xc3\xa9t\xc3\n"; printf "last"'
    wait_state "$D" done
    off=0
    : > "$TMP/all"
    for _ in $(seq 1 20); do
        read_step "$D" "$off" 3
        cat "$TMP/bytes" >> "$TMP/all"
        off="${header##*off=}"
        [[ "$header" == *"state=done"* ]] && break
    done
    printf 'a\xff\xfeb\n\xc3\xa9t\xc3\nlast' > "$TMP/expected"
    cmp "$TMP/all" "$TMP/expected"
    [ "$off" -eq "$(stat -c %s "$TMP/expected")" ]
}

@test "the end of a finished step comes in reads of at most max bytes" {
    start "$D" 'printf "0123456789\n0123456789\n"'
    wait_state "$D" done
    read_step "$D" 0 15
    # More is left: the state still reads running, at the last newline.
    [[ "$header" == *"state=running exit=- timedout=0 off=11" ]]
    read_step "$D" 11 15
    [[ "$header" == *"state=done exit=0 timedout=0 off=22" ]]
}

@test "a step whose process was killed reads lost" {
    start "$D" 'sleep 30'
    read -r pid _ _ < "$D/pid"
    kill -KILL "$pid"
    pkill -KILL -s "$pid" || true
    wait_state "$D" lost
    [[ "$header" == "ic-detached v1 state=lost exit=- timedout=0 off=0" ]]
}

@test "no directory, or no pid in it, reads none" {
    read_step "$D" 5
    [ "$header" = "ic-detached v1 state=none exit=- timedout=0 off=5" ]
    mkdir -m 700 "$D"
    cp "$SCRIPT" "$D/ctl.sh"
    read_step "$D" 0
    [ "$header" = "ic-detached v1 state=none exit=- timedout=0 off=0" ]
}

@test "the cap cuts the step: exit 124 and timedout" {
    start "$D" 'echo going; sleep 30' 1
    wait_state "$D" done
    [[ "$header" == "ic-detached v1 state=done exit=124 timedout=1 off="* ]]
    [ -e "$D/timedout" ]
}

@test "stop sends TERM to the whole session, then KILL to a child that ignores it" {
    start "$D" 'trap "" TERM; sleep 300 & wait; echo never'
    sleep 0.5
    read -r _ sid _ < "$D/pid"
    pgrep -s "$sid" >/dev/null
    run bash "$D/ctl.sh" stop "$D" 1
    [ "$status" -eq 0 ]
    ! pgrep -s "$sid" >/dev/null
    [ -e "$D/stopped" ]
    wait_state "$D" lost
}

@test "stop lets the step's trap run and its status be kept" {
    start "$D" 'trap "echo cleaned; exit 9" TERM; sleep 300 & wait'
    sleep 0.5
    run bash "$D/ctl.sh" stop "$D" 10
    [ "$status" -eq 0 ]
    wait_state "$D" done
    [[ "$header" == *"exit=9 timedout=0"* ]]
    grep -q cleaned "$TMP/bytes"
}

@test "stop with nothing alive does nothing" {
    run bash "$SCRIPT" stop "$D" 1
    [ "$status" -eq 0 ]
    start "$D" 'true'
    wait_state "$D" done
    run bash "$D/ctl.sh" stop "$D" 1
    [ "$status" -eq 0 ]
    [ ! -e "$D/stopped" ]
}

@test "a command's own timeout is not the cap: 124 before the deadline is no timedout" {
    start "$D" 'timeout 1 sleep 5'
    wait_state "$D" done
    [[ "$header" == "ic-detached v1 state=done exit=124 timedout=0 off="* ]]
    [ ! -e "$D/timedout" ]
}

@test "stop and clean leave alone a session whose leader's pid is another process now" {
    start "$D" 'true'
    wait_state "$D" done
    setsid sleep 300 </dev/null >/dev/null 2>&1 &
    stranger=$!
    sleep 0.2
    # The step's leader ended and its pid now names another session's.
    printf '%s %s 1\n' "$stranger" "$stranger" > "$D/pid"
    run bash "$D/ctl.sh" stop "$D" 1
    [ "$status" -eq 0 ]
    kill -0 "$stranger"
    [ ! -e "$D/stopped" ]
    run bash "$D/ctl.sh" clean "$D"
    [ "$status" -eq 0 ]
    [ ! -e "$D" ]
    kill -0 "$stranger"
    kill "$stranger"
}

@test "stop takes what is left of a session whose leader is gone" {
    start "$D" 'sleep 300 & wait'
    sleep 0.5
    read -r pid sid _ < "$D/pid"
    kill -KILL "$pid"
    sleep 0.2
    pgrep -s "$sid" >/dev/null
    run bash "$D/ctl.sh" clean "$D"
    [ "$status" -eq 75 ]
    run bash "$D/ctl.sh" stop "$D" 1
    [ "$status" -eq 0 ]
    ! pgrep -s "$sid" >/dev/null
    run bash "$D/ctl.sh" clean "$D"
    [ "$status" -eq 0 ]
}

@test "a start whose step does not start within 10 s removes its directory" {
    # A setsid that launches nothing: the step never writes its pid.
    printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/bin/setsid"
    chmod +x "$TMP/bin/setsid"
    run start "$D" 'echo never'
    [ "$status" -ne 0 ]
    [ "$status" -ne 73 ] && [ "$status" -ne 74 ] && [ "$status" -ne 78 ]
    [[ "$output" == *"did not start"* ]]
    [ ! -e "$D" ]
}

@test "a start without HOME is refused rather than locking in /tmp" {
    run env -u HOME bash "$SCRIPT" start "$D" 60 instance:7 41 Build 'echo never'
    [ "$status" -ne 0 ]
    [[ "$output" == *"HOME"* ]]
    [ ! -e "$D" ]
}

@test "clean refuses a directory whose name is not a step's" {
    mkdir -m 700 "$TMP/precious"
    : > "$TMP/precious/keep"
    run bash "$SCRIPT" clean "$TMP/precious"
    [ "$status" -eq 2 ]
    [ -e "$TMP/precious/keep" ]
}

@test "a live step of another job on the same scope refuses the start: 73" {
    start "$OTHER" 'sleep 30'
    run start "$D" 'echo should not run'
    [ "$status" -eq 73 ]
    [[ "$output" == *"instance:7"*"41 Build"* ]]
    [ ! -e "$D" ]
    # Another scope is free.
    run start "$D" 'true' 60 "instance:8"
    [ "$status" -eq 0 ]
}

@test "a hidden job's step and a visible one of the same instance run side by side" {
    start "$OTHER" 'sleep 30' 60 "instance:7:pitr_base_backup"
    run start "$D" 'true'
    [ "$status" -eq 0 ]
}

@test "a finished step of another job holds nothing" {
    start "$OTHER" 'true'
    wait_state "$OTHER" done
    run start "$D" 'true'
    [ "$status" -eq 0 ]
}

@test "a server that kills the account's processes at logout refuses: 78" {
    export KILL_USER_PROCESSES=true
    run start "$D" 'true'
    [ "$status" -eq 78 ]
    [[ "$output" == *"KillUserProcesses"* ]]
    [ ! -e "$D" ]
}

@test "a server without a tool the orders need refuses: 79" {
    local tool name bare="$TMP/bare"
    for tool in flock setsid timeout pgrep pkill; do
        mkdir -p "$bare"
        # Every command of the PATH but this one.
        for name in /usr/bin/* /bin/* "$TMP"/bin/*; do
            [ "$(basename "$name")" = "$tool" ] && continue
            [ -d "$name" ] && continue
            ln -sfn "$name" "$bare/$(basename "$name")"
        done
        PATH="$bare" run "$BASH" "$SCRIPT" start "$D" 60 instance:7 41 Build 'true'
        [ "$status" -eq 79 ]
        [[ "$output" == *"lacks $tool"* ]]
        [ ! -e "$D" ]
        rm -rf "$bare"
    done
}

@test "a path that is not this account's directory refuses: 74" {
    ln -s "$TMP/elsewhere" "$D"
    run start "$D" 'true'
    [ "$status" -eq 74 ]
    rm "$D"
    : > "$D"
    run start "$D" 'true'
    [ "$status" -eq 74 ]
    rm "$D"
}

@test "clean keeps a step that still runs and removes one that ended" {
    start "$D" 'sleep 30'
    run bash "$D/ctl.sh" clean "$D"
    [ "$status" -eq 75 ]
    [ -d "$D" ]
    bash "$D/ctl.sh" stop "$D" 5
    run bash "$D/ctl.sh" clean "$D"
    [ "$status" -eq 0 ]
    [ ! -e "$D" ]
    run bash "$SCRIPT" clean "$D"
    [ "$status" -eq 0 ]
}

@test "the result is the tail of stdout only" {
    start "$D" 'head -c 300000 /dev/zero | tr "\0" "x"; echo; echo tail; echo noise >&2'
    wait_state "$D" done
    run bash "$D/ctl.sh" result "$D"
    [ "${#output}" -le 262144 ]
    [[ "$output" == *"tail" ]]
    [[ "$output" != *"noise"* ]]
}

@test "a wrong order or argument is refused" {
    run bash "$SCRIPT" nope
    [ "$status" -eq 2 ]
    run bash "$SCRIPT" start "$D" soon instance:7 41 Build true
    [ "$status" -eq 2 ]
    run bash "$SCRIPT" read "$D" -1
    [ "$status" -eq 2 ]
}
