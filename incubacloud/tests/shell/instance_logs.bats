#!/usr/bin/env bats
# Tests for scripts/instance_logs.sh.
#
# ``sudo``, ``logrotate`` and ``apt-get`` are stubbed on PATH so the
# script's own logic is what gets exercised, and the logrotate config
# directory is redirected with IC_LOGROTATE_DIR so nothing is written
# to /etc.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/instance_logs.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME/project/inst"

    export IC_LOGROTATE_DIR="$TMP/logrotate.d"
    mkdir -p "$IC_LOGROTATE_DIR"

    mkdir -p "$TMP/bin"
    # sudo: run the command as-is (the test user cannot really escalate).
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
exec "$@"
STUB
    # logrotate: record the argv, succeed.
    cat > "$TMP/bin/logrotate" <<'STUB'
#!/usr/bin/env bash
echo "logrotate $*" >> "$CALLS"
STUB
    cat > "$TMP/bin/apt-get" <<'STUB'
#!/usr/bin/env bash
echo "apt-get $*" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/sudo" "$TMP/bin/logrotate" "$TMP/bin/apt-get"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    export PATH="$TMP/bin:$PATH"
    CONF="$IC_LOGROTATE_DIR/incubacloud-inst"
}

teardown() {
    rm -rf "$TMP"
}

@test "install creates the log directory, expanding the tilde" {
    run bash "$SCRIPT" install "~/project/inst" inst 60
    [ "$status" -eq 0 ]
    [ -d "$HOME/project/inst/logs" ]
}

@test "install is idempotent" {
    run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    [ "$status" -eq 0 ]
    run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    [ "$status" -eq 0 ]
    [ -d "$HOME/project/inst/logs" ]
}

@test "install writes a logrotate config for the absolute log path" {
    run bash "$SCRIPT" install "~/project/inst" inst 60
    [ -f "$CONF" ]
    grep -q "$HOME/project/inst/logs/odoo.log {" "$CONF"
}

@test "the config rotates daily, dated, compressed, and does not recreate" {
    run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    grep -q '^\s*daily' "$CONF"
    grep -q '^\s*dateext' "$CONF"
    grep -q '^\s*compress' "$CONF"
    grep -q '^\s*delaycompress' "$CONF"
    grep -q '^\s*missingok' "$CONF"
    # nocreate: Odoo's WatchedFileHandler reopens the file itself, owned
    # by the container's uid. A file created by logrotate would be
    # root's and the container could not write to it.
    grep -q '^\s*nocreate' "$CONF"
}

@test "retention comes from the argument" {
    run bash "$SCRIPT" install "$HOME/project/inst" inst 45
    grep -q '^\s*rotate 45' "$CONF"
}

@test "install validates the config it wrote" {
    run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    grep -q "logrotate .*-d .*incubacloud-inst" "$CALLS"
}

@test "install installs logrotate when the host lacks it" {
    # A minimal PATH holding only the tools the script needs — and no
    # logrotate — so "the host lacks it" is true regardless of where
    # the machine running these tests keeps its own copy.
    MIN="$TMP/min"
    mkdir -p "$MIN"
    for tool in bash dirname id mkdir stat chmod mktemp cat rm cp grep env; do
        ln -sf "$(command -v "$tool")" "$MIN/$tool"
    done
    ln -sf "$TMP/bin/sudo" "$MIN/sudo"
    ln -sf "$TMP/bin/apt-get" "$MIN/apt-get"
    PATH="$MIN" run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    [ "$status" -eq 0 ]
    grep -q "apt-get" "$CALLS"
}

@test "a failing chown warns but does not fail the deploy" {
    # Report the directory as owned by root so the script tries the
    # chown at all (the user running these tests may well be uid 1000),
    # and make that chown fail the way an unprivileged host would.
    cat > "$TMP/bin/stat" <<'STUB'
#!/usr/bin/env bash
echo 0
STUB
    cat > "$TMP/bin/chown" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
    chmod +x "$TMP/bin/stat" "$TMP/bin/chown"
    run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    [ "$status" -eq 0 ]
    [[ "$output" == *"WARNING"* ]]
    [ -d "$HOME/project/inst/logs" ]
}

@test "install refuses an instance name that is not shell-safe" {
    run bash "$SCRIPT" install "$HOME/project/inst" 'inst;rm -rf /' 60
    [ "$status" -ne 0 ]
    [ ! -f "$IC_LOGROTATE_DIR/incubacloud-inst;rm -rf /" ]
}

@test "install refuses a retention that is not a positive integer" {
    run bash "$SCRIPT" install "$HOME/project/inst" inst 0
    [ "$status" -ne 0 ]
}

@test "install fails when the instance directory is missing" {
    run bash "$SCRIPT" install "$HOME/project/nope" nope 60
    [ "$status" -ne 0 ]
}

@test "remove deletes the config" {
    run bash "$SCRIPT" install "$HOME/project/inst" inst 60
    [ -f "$CONF" ]
    run bash "$SCRIPT" remove inst
    [ "$status" -eq 0 ]
    [ ! -f "$CONF" ]
}

@test "remove is idempotent" {
    run bash "$SCRIPT" remove inst
    [ "$status" -eq 0 ]
}

@test "an unknown operation is an error" {
    run bash "$SCRIPT" frobnicate inst
    [ "$status" -ne 0 ]
}
