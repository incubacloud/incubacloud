#!/usr/bin/env bats
# Tests for ansible/playbooks/files/ssh_put_back.sh, which the hardening's
# rescue runs, and the timer it arms on the host when its connection is
# lost while sshd moves.
#
# ``systemctl`` is stubbed: it records each call in $CALLS. /etc is a
# temporary one (IC_SSH_PUT_BACK_ROOT); the kept copy is laid out as the
# play's "Keep the SSH configuration as it is" leaves it. Three hosts: an
# Ubuntu 24.04 one on its socket, an Ubuntu 22.04 or Debian one on the
# classic service, and one hardened before, whose own files come back.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../ansible/playbooks/files/ssh_put_back.sh"
    TMP="$(mktemp -d)"
    export IC_SSH_PUT_BACK_ROOT="$TMP/root"
    KEEP="$TMP/keep"
    SSHD_D="$IC_SSH_PUT_BACK_ROOT/etc/ssh/sshd_config.d"
    SOCKET_D="$IC_SSH_PUT_BACK_ROOT/etc/systemd/system/ssh.socket.d"
    mkdir -p "$TMP/bin" "$KEEP" "$SSHD_D" "$SOCKET_D"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    cat > "$TMP/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/systemctl"
    export PATH="$TMP/bin:$PATH"
    # What the run wrote before it failed: our sshd file and the socket's
    # port drop-in, beside a file of the image's own.
    echo "Port 24680" > "$SSHD_D/00-incubacloud-hardening.conf"
    echo "PasswordAuthentication yes" > "$SSHD_D/50-cloud-init.conf"
    printf '[Socket]\nListenStream=\nListenStream=24680\n' \
        > "$SOCKET_D/zz-incubacloud-port.conf"
}

teardown() {
    rm -rf "$TMP"
}

@test "Ubuntu 24.04 on its socket: our files go, the socket comes back after the service stops" {
    printf 'ssh.socket enabled active\nssh.service disabled inactive\n' > "$KEEP/units"
    run sh "$SCRIPT" "$KEEP"
    [ "$status" -eq 0 ]
    [ ! -e "$SSHD_D/00-incubacloud-hardening.conf" ]
    [ -e "$SSHD_D/50-cloud-init.conf" ]
    [ ! -e "$SOCKET_D" ]
    [ "$(cat "$CALLS")" = "daemon-reload
enable ssh.socket
disable ssh.service
stop ssh.service
restart ssh.socket" ]
    [[ "$output" == *"SSH configuration put back: ssh.socket enabled active;"* ]]
    [ -e "$KEEP/put-back-ran" ]
}

@test "Ubuntu 22.04 or Debian on the classic service: the socket stops, the service restarts" {
    printf 'ssh.socket disabled inactive\nssh.service enabled active\n' > "$KEEP/units"
    run sh "$SCRIPT" "$KEEP"
    [ "$status" -eq 0 ]
    [ ! -e "$SSHD_D/00-incubacloud-hardening.conf" ]
    [ ! -e "$SOCKET_D" ]
    [ "$(cat "$CALLS")" = "daemon-reload
disable ssh.socket
enable ssh.service
stop ssh.socket
restart ssh.service" ]
}

@test "a host hardened before gets its own files back, under the names they had" {
    printf 'ssh.socket enabled active\nssh.service static inactive\n' > "$KEEP/units"
    printf 'Port 22626\nPermitRootLogin no\n' > "$KEEP/00-incubacloud-hardening.conf"
    mkdir "$KEEP/ssh.socket.d"
    printf '[Socket]\nListenStream=\nListenStream=22626\n' \
        > "$KEEP/ssh.socket.d/10-incubacloud-port.conf"
    echo "Port 22626" > "$SSHD_D/99-incubacloud-hardening.conf"
    run sh "$SCRIPT" "$KEEP"
    [ "$status" -eq 0 ]
    [ "$(cat "$SSHD_D/00-incubacloud-hardening.conf")" = "Port 22626
PermitRootLogin no" ]
    [ ! -e "$SSHD_D/99-incubacloud-hardening.conf" ]
    [ "$(ls "$SOCKET_D")" = "10-incubacloud-port.conf" ]
    grep -qx "ListenStream=22626" "$SOCKET_D/10-incubacloud-port.conf"
    # A static unit is left as it is; only the socket is enabled.
    [ "$(cat "$CALLS")" = "daemon-reload
enable ssh.socket
stop ssh.service
restart ssh.socket" ]
}

@test "without the kept units nothing was changed, and nothing is touched" {
    run sh "$SCRIPT" "$KEEP"
    [ "$status" -eq 0 ]
    [[ "$output" == "Nothing to put back"* ]]
    [ -e "$SSHD_D/00-incubacloud-hardening.conf" ]
    [ -e "$SOCKET_D/zz-incubacloud-port.conf" ]
    [ ! -s "$CALLS" ]
    [ ! -e "$KEEP/put-back-ran" ]
}

@test "run twice, as the timer and then the rescue may, it ends the same" {
    printf 'ssh.socket enabled active\nssh.service disabled inactive\n' > "$KEEP/units"
    printf 'Port 22626\n' > "$KEEP/00-incubacloud-hardening.conf"
    run sh "$SCRIPT" "$KEEP"
    [ "$status" -eq 0 ]
    run sh "$SCRIPT" "$KEEP"
    [ "$status" -eq 0 ]
    [ "$(cat "$SSHD_D/00-incubacloud-hardening.conf")" = "Port 22626" ]
    [ ! -e "$SOCKET_D" ]
}
