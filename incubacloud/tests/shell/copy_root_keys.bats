#!/usr/bin/env bats
# Tests for ansible/playbooks/files/copy_root_keys.sh, the step of
# host_hardening.yml that gives the operator account root's keys.
#
# Root's and the operator's files are files of this test; ``chown`` records
# its call (the test does not run as root) and fails with STUB_CHOWN_FAILS.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../ansible/playbooks/files/copy_root_keys.sh"
    TMP="$(mktemp -d)"
    export TMP
    mkdir -p "$TMP/bin" "$TMP/root/.ssh" "$TMP/admin/.ssh"
    cat > "$TMP/bin/chown" <<'STUB'
#!/usr/bin/env bash
echo "chown $*" >> "$TMP/calls"
[ -z "${STUB_CHOWN_FAILS:-}" ] || exit 1
STUB
    chmod +x "$TMP/bin/"*
    export PATH="$TMP/bin:$PATH"
    : > "$TMP/calls"

    PANEL="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPanelKeyPanelKeyPanelKeyPanelKey/+="
    OWNER="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwnerKeyOwnerKeyOwnerKeyOwnerKey"
    OTHER="ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQOtherKeyOtherKeyOtherKey=="
    # What cloud-init writes on root when the image lets in another user.
    FORCED="no-port-forwarding,no-agent-forwarding,no-X11-forwarding,command=\"echo 'Please login as the user admin rather than the user root.';echo;sleep 10;exit 142\""
    export IC_OPERATOR_USER=admin
    export IC_OPERATOR_KEYS="$TMP/admin/.ssh/authorized_keys"
    export IC_ROOT_KEYS="$TMP/root/.ssh/authorized_keys"
}

teardown() {
    rm -rf "$TMP"
}

# keys FILE LINE...: write FILE, mode 600.
keys() {
    local file=$1
    shift
    printf '%s\n' "$@" > "$file"
    chmod 600 "$file"
}

@test "a reused account keeps its own line for a key root has with a forced command" {
    keys "$IC_ROOT_KEYS" "$FORCED $PANEL panel"
    keys "$IC_OPERATOR_KEYS" "$PANEL panel"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$PANEL panel" ]
    [ ! -s "$TMP/calls" ]
    [[ "$output" == *"No key of root's was missing from $IC_OPERATOR_KEYS."* ]]
}

@test "a new operator gets root's keys, owned by it and mode 600" {
    keys "$IC_ROOT_KEYS" "# root's keys" "$PANEL panel" "" "from=\"10.0.0.1\" $OWNER owner"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$(printf '%s\n' "$PANEL panel" "from=\"10.0.0.1\" $OWNER owner")" ]
    [ "$(stat -c %a "$IC_OPERATOR_KEYS")" = 600 ]
    grep -q "^chown admin: $TMP/admin/.ssh/authorized_keys\." "$TMP/calls"
    [[ "$output" == *"Added 2 key(s) of root's to $IC_OPERATOR_KEYS."* ]]
}

@test "the keys root has and the operator lacks are added after its own, which stay" {
    keys "$IC_ROOT_KEYS" "$PANEL root-comment" "$OWNER owner"
    keys "$IC_OPERATOR_KEYS" "# mine" "no-pty $PANEL panel" "$OTHER other"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$(printf '%s\n' "# mine" "no-pty $PANEL panel" "$OTHER other" "$OWNER owner")" ]
    [[ "$output" == *"Added 1 key(s) of root's"* ]]
}

@test "a line with a forced command is never copied, and that is said" {
    keys "$IC_ROOT_KEYS" "$FORCED $OWNER owner" "restrict,COMMAND=\"/usr/bin/true\" $OTHER backup" "$PANEL panel"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$PANEL panel" ]
    [[ "$output" == *"Left out 2 key(s) of root's with a forced command"* ]]
    [[ "$output" == *"Added 1 key(s) of root's"* ]]
}

@test "an operator's last line without its newline is not run into" {
    keys "$IC_ROOT_KEYS" "$OWNER owner"
    printf '%s' "$PANEL panel" > "$IC_OPERATOR_KEYS"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$(printf '%s\n' "$PANEL panel" "$OWNER owner")" ]
}

@test "a key root lists twice is added once" {
    keys "$IC_ROOT_KEYS" "$OWNER one" "$OWNER two"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$OWNER one" ]
}

@test "a second run adds nothing and writes nothing" {
    keys "$IC_ROOT_KEYS" "$PANEL panel" "$OWNER owner"
    run bash "$SCRIPT"
    : > "$TMP/calls"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ ! -s "$TMP/calls" ]
    [[ "$output" != *"Added"* ]]
}

@test "root without authorized_keys leaves the operator's as it is" {
    keys "$IC_OPERATOR_KEYS" "$PANEL panel"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$PANEL panel" ]
    [[ "$output" == *"root has no authorized_keys"* ]]
}

@test "a linked authorized_keys is left as it is" {
    keys "$IC_ROOT_KEYS" "$OWNER owner"
    keys "$TMP/elsewhere" "$PANEL panel"
    ln -s "$TMP/elsewhere" "$IC_OPERATOR_KEYS"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -L "$IC_OPERATOR_KEYS" ]
    [ "$(cat "$TMP/elsewhere")" = "$PANEL panel" ]
    [[ "$output" == *"is a link"* ]]
}

@test "lines that do not read as a key are not copied" {
    keys "$IC_ROOT_KEYS" "not a key at all" "ssh-ed25519 AAAA'\$(touch $TMP/pwned)" "$OWNER owner"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$OWNER owner" ]
    [ ! -e "$TMP/pwned" ]
}

@test "a file that cannot be written fails the step and keeps the old one" {
    keys "$IC_ROOT_KEYS" "$OWNER owner"
    keys "$IC_OPERATOR_KEYS" "$PANEL panel"
    STUB_CHOWN_FAILS=1 run bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [ "$(cat "$IC_OPERATOR_KEYS")" = "$PANEL panel" ]
    [ "$(find "$TMP/admin/.ssh" -name 'authorized_keys.*' | wc -l)" -eq 0 ]
    [[ "$output" == *"could not be added"* ]]
}
