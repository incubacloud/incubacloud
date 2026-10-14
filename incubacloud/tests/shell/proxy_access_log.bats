#!/usr/bin/env bats
# Tests for scripts/proxy_access_log.sh.
#
# ``sudo``, ``logrotate`` and ``apt-get`` are stubbed on PATH so the
# script's own logic is what gets exercised; the log and logrotate
# directories are redirected so nothing is written to /etc or /var/log.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/proxy_access_log.sh"
    TMP="$(mktemp -d)"
    export IC_LOGROTATE_DIR="$TMP/logrotate.d"
    export IC_PROXY_LOG_DIR="$TMP/log/traefik"
    export IC_SYSTEMD_DIR="$TMP/systemd"
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
STUB
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
exec "$@"
STUB
    cat > "$TMP/bin/logrotate" <<'STUB'
#!/usr/bin/env bash
echo "logrotate $*" >> "$CALLS"
STUB
    cat > "$TMP/bin/apt-get" <<'STUB'
#!/usr/bin/env bash
echo "apt-get $*" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/sudo" "$TMP/bin/logrotate" "$TMP/bin/apt-get" "$TMP/bin/systemctl"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    export PATH="$TMP/bin:$PATH"
    CONF="$IC_LOGROTATE_DIR/incubacloud-traefik"
}

teardown() {
    rm -rf "$TMP"
}

@test "install creates the log directory" {
    run "$SCRIPT" install
    [ "$status" -eq 0 ]
    [ -d "$IC_PROXY_LOG_DIR" ]
}

@test "install writes a logrotate entry for the access log" {
    run "$SCRIPT" install
    [ "$status" -eq 0 ]
    [ -f "$CONF" ]
    grep -q "^$IC_PROXY_LOG_DIR/access.log {" "$CONF"
}

@test "the entry keeps 30 days by age, whatever the number of files" {
    "$SCRIPT" install
    grep -q "maxage 30" "$CONF"
    grep -q "maxsize 100M" "$CONF"
    grep -q "compress" "$CONF"
    # A busy host rotating on size several times a day must not run out
    # of files before the 30 days: the count is far above that.
    run grep -E "^\s+rotate [0-9]+" "$CONF"
    [ "$status" -eq 0 ]
    count="$(grep -E '^\s+rotate [0-9]+' "$CONF" | awk '{print $2}')"
    [ "$count" -ge 300 ]
}

@test "two rotations in one day get different names" {
    "$SCRIPT" install
    grep -q "dateformat .%Y-%m-%d-%s" "$CONF"
}

@test "the proxy is told to reopen the file after rotating" {
    "$SCRIPT" install
    grep -q "docker kill --signal=USR1 inverseproxy-proxy-1" "$CONF"
}

@test "the entry is checked with logrotate" {
    "$SCRIPT" install
    grep -q "logrotate -d $CONF" "$CALLS"
}

@test "running it twice leaves one identical entry" {
    "$SCRIPT" install
    first="$(cat "$CONF")"
    "$SCRIPT" install
    [ "$(cat "$CONF")" = "$first" ]
    [ "$(ls "$IC_LOGROTATE_DIR" | wc -l)" -eq 1 ]
}

@test "the size limit is looked at every ten minutes, not once a day" {
    "$SCRIPT" install
    grep -q "^OnCalendar=\*:0/10" "$IC_SYSTEMD_DIR/incubacloud-traefik-logrotate.timer"
    grep -q "^ExecStart=.*logrotate $CONF\$" "$IC_SYSTEMD_DIR/incubacloud-traefik-logrotate.service"
    grep -q "systemctl daemon-reload" "$CALLS"
    grep -q "systemctl enable --now incubacloud-traefik-logrotate.timer" "$CALLS"
}

@test "the timer runs the same entry the daily run does, with its state" {
    # One state file for both: that is how each knows the other rotated
    # today, so a second rotation at midnight cannot happen.
    "$SCRIPT" install
    run grep -- "--state" "$IC_SYSTEMD_DIR/incubacloud-traefik-logrotate.service"
    [ "$status" -ne 0 ]
}

@test "an unknown operation fails" {
    run "$SCRIPT" remove
    [ "$status" -ne 0 ]
}
