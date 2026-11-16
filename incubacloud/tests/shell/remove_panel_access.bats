#!/usr/bin/env bats
# Tests for ansible/playbooks/files/remove_panel_access.sh, the last step
# of host_teardown.yml on a server.
#
# The accounts are lines of $TMP/passwd, their homes directories of this
# test. ``getent`` and ``id`` answer from that file, ``userdel`` records
# its call and removes the line (or fails, with STUB_USERDEL_FAILS), and
# ``sudo -l -U ACCOUNT`` prints the account's own rules ($TMP/sudo/ACCOUNT)
# and, while the grant (IC_SUDOERS_FILE, a file of this test) names it,
# the grant's.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../ansible/playbooks/files/remove_panel_access.sh"
    TMP="$(mktemp -d)"
    export TMP
    mkdir -p "$TMP/bin" "$TMP/sudo" "$TMP/home/root/.ssh" "$TMP/home/incubacloud/.ssh" \
        "$TMP/home/ubuntu/.ssh"
    cat > "$TMP/passwd" <<EOF
root:x:0:0::$TMP/home/root:/bin/bash
incubacloud:x:1000:1000::$TMP/home/incubacloud:/bin/bash
ubuntu:x:1001:1001::$TMP/home/ubuntu:/bin/bash
EOF
    cat > "$TMP/bin/getent" <<'STUB'
#!/usr/bin/env bash
if [ -n "${2:-}" ]; then grep "^$2:" "$TMP/passwd"; else cat "$TMP/passwd"; fi
STUB
    cat > "$TMP/bin/id" <<'STUB'
#!/usr/bin/env bash
grep -q "^$2:" "$TMP/passwd"
STUB
    cat > "$TMP/bin/userdel" <<'STUB'
#!/usr/bin/env bash
echo "userdel $*" >> "$TMP/calls"
[ -z "${STUB_USERDEL_FAILS:-}" ] || exit 8
user=${*: -1}
grep -v "^$user:" "$TMP/passwd" > "$TMP/passwd.new"
mv "$TMP/passwd.new" "$TMP/passwd"
STUB
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
echo "sudo $*" >> "$TMP/calls"
user=${*: -1}
[ ! -f "$TMP/sudo/$user" ] || cat "$TMP/sudo/$user"
if [ -f "$IC_SUDOERS_FILE" ] && grep -q "^$user " "$IC_SUDOERS_FILE"; then
    echo "    (ALL) NOPASSWD: ALL"
fi
exit 0
STUB
    chmod +x "$TMP/bin/"*
    export PATH="$TMP/bin:$PATH"
    : > "$TMP/calls"

    PANEL="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPanelKeyPanelKeyPanelKeyPanelKey/+="
    OWNER="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwnerKeyOwnerKeyOwnerKeyOwnerKey"
    export IC_PANEL_KEY="$PANEL incubacloud"
    export IC_OPERATOR_USER=incubacloud
    export IC_SUDOERS_FILE="$TMP/incubacloud-system"
}

teardown() {
    rm -rf "$TMP"
}

# keys ACCOUNT LINE...: write the account's authorized_keys, mode 600.
keys() {
    local file="$TMP/home/$1/.ssh/authorized_keys"
    shift
    printf '%s\n' "$@" > "$file"
    chmod 600 "$file"
}

grant() {
    echo "$1 ALL=(ALL) NOPASSWD: ALL" > "$IC_SUDOERS_FILE"
}

# own_rule ACCOUNT RULE: what sudo lists for the account without the grant.
own_rule() {
    echo "    $2" > "$TMP/sudo/$1"
}

@test "a hardened server only the panel logged in to: key, account and grant go" {
    keys root "$PANEL incubacloud"
    keys incubacloud "$PANEL incubacloud"
    grant incubacloud
    IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ ! -s "$TMP/home/root/.ssh/authorized_keys" ]
    [ ! -s "$TMP/home/incubacloud/.ssh/authorized_keys" ]
    grep -q "^userdel -f incubacloud$" "$TMP/calls"
    ! grep -q "^incubacloud:" "$TMP/passwd"
    [ -d "$TMP/home/incubacloud" ]
    [ ! -e "$IC_SUDOERS_FILE" ]
    [[ "$output" == *"Removed the account incubacloud; its home stays."* ]]
    [[ "$output" == *"Removed the sudo grant $IC_SUDOERS_FILE."* ]]
}

@test "the owner's own key in the operator account keeps the account and its grant" {
    keys root "$OWNER owner@laptop" "$PANEL incubacloud"
    keys incubacloud "$OWNER owner@laptop" "$PANEL incubacloud"
    grant incubacloud
    IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/home/root/.ssh/authorized_keys")" = "$OWNER owner@laptop" ]
    [ "$(cat "$TMP/home/incubacloud/.ssh/authorized_keys")" = "$OWNER owner@laptop" ]
    ! grep -q userdel "$TMP/calls"
    grep -q "^incubacloud:" "$TMP/passwd"
    [ "$(cat "$IC_SUDOERS_FILE")" = "incubacloud ALL=(ALL) NOPASSWD: ALL" ]
    [ ! -e "$IC_SUDOERS_FILE.removing" ]
    [[ "$output" == *"Kept the account incubacloud: other keys log in with it."* ]]
    [[ "$output" == *"Kept the sudo grant $IC_SUDOERS_FILE: without it incubacloud could not"* ]]
}

@test "a user made by hand for the panel, with its own sudo rule, stays and loses the grant" {
    # As the documentation for connecting a server shows: the account
    # named like the operator, with a rule of its own and the panel's key.
    keys incubacloud "$PANEL incubacloud"
    grant incubacloud
    own_rule incubacloud "(ALL) NOPASSWD: ALL"
    IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ ! -s "$TMP/home/incubacloud/.ssh/authorized_keys" ]
    ! grep -q userdel "$TMP/calls"
    grep -q "^incubacloud:" "$TMP/passwd"
    [ ! -e "$IC_SUDOERS_FILE" ]
    [[ "$output" == *"Kept the account incubacloud: it has a sudo rule of its own"* ]]
    [[ "$output" == *"incubacloud runs sudo without a password by a rule of its own."* ]]
}

@test "the panel's line goes whatever its options and comment, the others stay in order" {
    keys root "# the owner's keys" "$OWNER a" \
        "from=\"10.0.0.1\",no-pty $PANEL some comment" "$OWNER b"
    IC_CONNECT_USER=root IC_OPERATOR_USER="" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/home/root/.ssh/authorized_keys")" = "$(printf '%s\n' "# the owner's keys" "$OWNER a" "$OWNER b")" ]
    [ "$(stat -c %a "$TMP/home/root/.ssh/authorized_keys")" = 600 ]
    [[ "$output" == *"Removed the panel's key from $TMP/home/root/.ssh/authorized_keys."* ]]
}

@test "a key that only starts like the panel's stays" {
    keys root "${PANEL}xyz other"
    IC_CONNECT_USER=root IC_OPERATOR_USER="" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/home/root/.ssh/authorized_keys")" = "${PANEL}xyz other" ]
    [[ "$output" != *"Removed"* ]]
}

@test "an account the server had keeps its own sudo, loses the panel's key and the grant" {
    rm -r "$TMP/home/incubacloud"
    sed -i '/^incubacloud:/d' "$TMP/passwd"
    keys ubuntu "$OWNER owner" "$PANEL incubacloud"
    grant ubuntu
    own_rule ubuntu "(ALL) NOPASSWD:ALL"
    IC_CONNECT_USER=ubuntu run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/home/ubuntu/.ssh/authorized_keys")" = "$OWNER owner" ]
    ! grep -q userdel "$TMP/calls"
    grep -q "^sudo -n -l -U ubuntu$" "$TMP/calls"
    [ ! -e "$IC_SUDOERS_FILE" ]
    [ ! -e "$IC_SUDOERS_FILE.removing" ]
    [[ "$output" == *"ubuntu runs sudo without a password by a rule of its own."* ]]
}

@test "an account that would lose sudo without the grant keeps it" {
    sed -i '/^incubacloud:/d' "$TMP/passwd"
    keys ubuntu "$OWNER owner" "$PANEL incubacloud"
    grant ubuntu
    own_rule ubuntu "(ALL : ALL) ALL"
    IC_CONNECT_USER=ubuntu run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_SUDOERS_FILE")" = "ubuntu ALL=(ALL) NOPASSWD: ALL" ]
    [ ! -e "$IC_SUDOERS_FILE.removing" ]
    [[ "$output" == *"Kept the sudo grant $IC_SUDOERS_FILE: without it ubuntu could not"* ]]
}

@test "a second run finds nothing to do and says nothing was removed" {
    keys root "$PANEL incubacloud"
    keys incubacloud "$PANEL incubacloud"
    grant incubacloud
    IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    : > "$TMP/calls"
    IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ ! -s "$TMP/calls" ]
    [[ "$output" != *"Removed"* ]]
}

@test "no key: the account and the grant are still judged" {
    keys incubacloud "$OWNER owner"
    grant incubacloud
    IC_PANEL_KEY="" IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"there is no key to remove"* ]]
    [[ "$output" == *"Kept the account incubacloud"* ]]
    [ "$(cat "$TMP/home/incubacloud/.ssh/authorized_keys")" = "$OWNER owner" ]
}

@test "a key that does not read as one removes nothing" {
    keys root "$PANEL incubacloud"
    IC_PANEL_KEY="ssh-ed25519 AAAA'\$(touch $TMP/pwned)" IC_CONNECT_USER=root \
        IC_OPERATOR_USER="" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"does not read as a public key"* ]]
    [ "$(cat "$TMP/home/root/.ssh/authorized_keys")" = "$PANEL incubacloud" ]
    [ ! -e "$TMP/pwned" ]
}

@test "a linked authorized_keys is left as it is" {
    keys root "$PANEL incubacloud"
    ln -s "$TMP/home/root/.ssh/authorized_keys" "$TMP/home/ubuntu/.ssh/authorized_keys"
    IC_CONNECT_USER=ubuntu IC_OPERATOR_USER="" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -L "$TMP/home/ubuntu/.ssh/authorized_keys" ]
    [[ "$output" == *"$TMP/home/ubuntu/.ssh/authorized_keys is a link"* ]]
}

@test "a userdel that fails is said, puts the grant back and still exits 0" {
    keys incubacloud "$PANEL incubacloud"
    grant incubacloud
    STUB_USERDEL_FAILS=1 IC_CONNECT_USER=incubacloud run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"The account incubacloud could not be removed."* ]]
    [ -f "$IC_SUDOERS_FILE" ]
    [ ! -e "$IC_SUDOERS_FILE.removing" ]
}

@test "root is never removed, whatever the operator is called" {
    keys root "$PANEL incubacloud"
    IC_OPERATOR_USER=root IC_CONNECT_USER=root run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    ! grep -q userdel "$TMP/calls"
    grep -q "^root:" "$TMP/passwd"
}

@test "another account the image gave the panel's key loses it too, and nothing else" {
    mkdir -p "$TMP/home/debian/.ssh"
    echo "debian:x:1002:1002::$TMP/home/debian:/bin/bash" >> "$TMP/passwd"
    keys root "$PANEL incubacloud"
    keys debian "$OWNER owner" "no-pty $PANEL debian"
    IC_CONNECT_USER=root IC_OPERATOR_USER="" run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/home/debian/.ssh/authorized_keys")" = "$OWNER owner" ]
    [ ! -s "$TMP/home/root/.ssh/authorized_keys" ]
    grep -q "^debian:" "$TMP/passwd"
    [[ "$output" == *"Removed the panel's key from $TMP/home/debian/.ssh/authorized_keys."* ]]
}
