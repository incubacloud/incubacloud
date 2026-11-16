#!/usr/bin/env bash
# Take the panel's access off a server it no longer manages.
#
# Run as root by host_teardown.yml, as its last step on the server: once
# it has run, the panel can no longer log in there.
#
# Environment:
#   IC_PANEL_KEY      the panel's public key, "<type> <base64> [comment]";
#                     empty when the panel logs in without one
#   IC_CONNECT_USER   the account the panel connects as
#   IC_OPERATOR_USER  the account hardening creates when the panel
#                     connected as root
#   IC_SUDOERS_FILE   hardening's sudo grant, by default
#                     /etc/sudoers.d/incubacloud-system
#
# 1. The panel's key comes off the authorized_keys of every account: root,
#    the connecting account, the operator account, and any other a cloud
#    image gave the same key (Ubuntu's ``ubuntu`` on some providers). Each
#    line that holds its base64 as a field goes, whatever its options and
#    comment. The other lines stay, and the file keeps its owner and mode.
#    A file that is a link is left as it is, and that is said.
# 2. The operator account goes, its home kept, only when it is one the
#    panel made: no key is left in its authorized_keys, so nobody else
#    logs in with it, and without hardening's grant it has no
#    passwordless sudo, so the server's owner did not make it for the
#    panel (a user made by hand for the panel has a rule of its own, as
#    hardening requires of one). With a key left it stays: hardening
#    copied root's keys into it and turned root's own SSH login off, so it
#    is how the holders of those keys reach root.
# 3. Hardening's grant goes when the account it names no longer exists,
#    or still runs sudo without a password by a rule of its own. Otherwise
#    it stays, and that is said: it is how that account reaches root.
#
# Root's account, other accounts and their keys, sshd's configuration and
# the homes are never touched. Every step says what it did, and the
# script always exits 0: what it leaves never stops the teardown.
set -u

key=${IC_PANEL_KEY:-}
connect=${IC_CONNECT_USER:-}
operator=${IC_OPERATOR_USER:-}
sudoers=${IC_SUDOERS_FILE:-/etc/sudoers.d/incubacloud-system}
# The grant set aside, under a name sudo does not read (it holds a dot),
# while sudo is asked what the accounts can do without it.
aside=$sudoers.removing

# The key's base64, the one part of its line nobody rewrites.
body=$(printf '%s\n' "$key" | awk '{print $2}')
case $body in
    '')
        echo "The panel logs in without a key here: there is no key to remove." ;;
    *[!A-Za-z0-9+/=]*)
        echo "The panel's key does not read as a public key: no key was removed."
        body= ;;
esac

# home_of ACCOUNT: print the account's home, nothing for no such account.
home_of() {
    getent passwd "$1" | cut -d: -f6
}

# exists ACCOUNT: whether the account exists.
exists() {
    [ -n "$1" ] && id -u "$1" >/dev/null 2>&1
}

# holds_key FILE: whether a line of FILE has the key's base64 as a field.
holds_key() {
    awk -v b="$body" '{for (i = 1; i <= NF; i++) if ($i == b) found = 1}
        END {exit !found}' "$1"
}

# keys_left FILE: whether FILE has a line other than a comment or a blank.
keys_left() {
    grep -Eq '^[[:space:]]*[^#[:space:]]' "$1"
}

# own_sudo ACCOUNT: whether the account runs any command as root without a
# password by the rules sudo reads now.
own_sudo() {
    sudo -n -l -U "$1" 2>/dev/null | grep -q 'NOPASSWD: *ALL'
}

# strip_key HOME: remove the key's lines from HOME/.ssh/authorized_keys,
# keeping every other line, its owner and its mode.
strip_key() {
    local file tmp=
    file=$1/.ssh/authorized_keys
    if [ -L "$file" ]; then
        echo "$file is a link: it was left as it is."
        return 0
    fi
    if [ ! -f "$file" ] || ! holds_key "$file"; then
        return 0
    fi
    if tmp=$(mktemp "$file.XXXXXX") &&
        awk -v b="$body" '{for (i = 1; i <= NF; i++) if ($i == b) next}
            {print}' "$file" > "$tmp" &&
        chown --reference="$file" "$tmp" &&
        chmod --reference="$file" "$tmp" &&
        mv -f "$tmp" "$file"; then
        echo "Removed the panel's key from $file."
    else
        [ -z "$tmp" ] || rm -f "$tmp"
        echo "The panel's key could not be removed from $file: remove its line by hand."
    fi
}

if [ -n "$body" ]; then
    # Root, the connecting and the operator account first, then the rest;
    # each home once.
    { for account in root "$connect" "$operator"; do
          [ -z "$account" ] || home_of "$account"
      done
      getent passwd | cut -d: -f6; } | awk 'NF && !seen[$0]++' |
        while read -r home; do
            strip_key "$home"
        done
fi

grantee=
if [ -f "$sudoers" ]; then
    grantee=$(awk '!/^[[:space:]]*(#|$)/ {print $1; exit}' "$sudoers")
    if ! mv -f "$sudoers" "$aside"; then
        echo "The sudo grant $sudoers could not be removed."
        grantee=
    fi
fi

if [ "$operator" != root ] && exists "$operator"; then
    file=$(home_of "$operator")/.ssh/authorized_keys
    if [ -L "$file" ] || { [ -f "$file" ] && keys_left "$file"; }; then
        echo "Kept the account $operator: other keys log in with it."
    elif own_sudo "$operator"; then
        echo "Kept the account $operator: it has a sudo rule of its own, so the server's owner made it."
    # -f: the account is the one this very session runs as.
    elif userdel -f "$operator"; then
        echo "Removed the account $operator; its home stays."
    else
        echo "The account $operator could not be removed."
    fi
fi

if [ -e "$aside" ]; then
    if ! exists "$grantee"; then
        rm -f "$aside"
        echo "Removed the sudo grant $sudoers."
    elif own_sudo "$grantee"; then
        rm -f "$aside"
        echo "Removed the sudo grant $sudoers: $grantee runs sudo without a password by a rule of its own."
    else
        mv -f "$aside" "$sudoers"
        echo "Kept the sudo grant $sudoers: without it $grantee could not run sudo without a password."
    fi
fi
exit 0
