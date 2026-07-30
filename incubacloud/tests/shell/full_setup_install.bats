#!/usr/bin/env bats
# Tests for scripts/full_setup_install.sh.
#
# Every external command is stubbed on PATH so the script's own control
# flow is exercised: the idempotency guards (skip installs when a tool is
# already present) and the unconditional ``apt-get update``. ``sudo`` is
# stubbed to ``env "$@"`` so the ``sudo DEBIAN_FRONTEND=… apt-get …`` form
# resolves to the apt-get stub.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/full_setup_install.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME"
    mkdir -p "$TMP/bin"
    export APT_CALLS="$TMP/apt_calls"
    : > "$APT_CALLS"

    _stub() {  # name, body
        printf '#!/usr/bin/env bash\n%s\n' "$2" > "$TMP/bin/$1"
        chmod +x "$TMP/bin/$1"
    }
    # sudo → run the rest (env handles a VAR=val prefix then the command).
    _stub sudo 'exec env "$@"'
    _stub apt-get 'echo "apt-get $*" >> "$APT_CALLS"; exit 0'
    _stub curl 'exit 0'
    _stub dpkg 'exit 0'
    _stub usermod 'exit 0'
    _stub pipx 'exit 0'
    _stub python3 'exit 0'
    _stub zip 'exit 0'
    _stub groups 'echo "cloud docker"'
    _stub docker 'case "$*" in "--version") echo "Docker version 27";; "compose version") exit 0;; *) exit 0;; esac'
    _stub git 'case "$1" in "--version") echo "git version 2.4";; "config") exit 0;; *) exit 0;; esac'
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "runs to completion when every tool is already present" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Host toolchain installation complete."* ]]
}

@test "always refreshes the apt index" {
    run bash "$SCRIPT"
    grep -q "apt-get update" "$APT_CALLS"
}

@test "skips apt installs when the tools are already present (idempotent)" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    ! grep -q "apt-get install" "$APT_CALLS"
}
