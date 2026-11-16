#!/usr/bin/env bash
# Give the operator account the keys that log in as root, those it lacks.
#
# Run as root by host_hardening.yml before the panel turns root's own SSH
# login off: whoever reached root with a key reaches the operator account
# with it, and root through its sudo.
#
# Environment:
#   IC_OPERATOR_USER  the operator account
#   IC_OPERATOR_KEYS  its authorized_keys file
#   IC_ROOT_KEYS      root's, by default /root/.ssh/authorized_keys
#
# The operator's file is never written over. Each key of root's it lacks,
# matched by its base64 whatever its options and comment, is added at its
# end as root's line has it; every line it had stays. A line of root's with
# a forced command (``command=``) is left out: it opens no shell, and cloud
# images put one on root that only says to log in as another user
# ("Please login as the user ..."). Copied over the operator's own line for
# the same key, as a plain copy of root's file did, it left that account,
# and the panel connecting as it, with that message instead of a shell.
# Comments, blank lines and lines that do not read as a key are not copied.
# A file that is a link is left as it is, and that is said.
#
# The file it writes is the operator's, mode 0600. It says what it did, and
# exits 1 only when the file could not be written.
set -u

operator=${IC_OPERATOR_USER:?}
dest=${IC_OPERATOR_KEYS:?}
src=${IC_ROOT_KEYS:-/root/.ssh/authorized_keys}

if [ ! -f "$src" ]; then
    echo "root has no authorized_keys: there is no key to add to $dest."
    exit 0
fi
if [ -L "$dest" ]; then
    echo "$dest is a link: it was left as it is, without root's keys."
    exit 0
fi

# One line per line of root's: "add <line>" for a key the operator lacks,
# "forced" for one with a forced command. A key is the field that reads as
# a public key's base64 (every one starts AAAA, its type's length), and its
# options are among the fields before it.
plan=$(awk -v have_file="$dest" '
    function blob(   i) {
        for (i = 1; i <= NF; i++)
            if ($i ~ /^AAAA[0-9A-Za-z+\/]+=*$/) return i
        return 0
    }
    BEGIN {
        while ((getline < have_file) > 0)
            if ((k = blob())) have[$k] = 1
    }
    /^[[:space:]]*(#|$)/ { next }
    {
        k = blob()
        if (!k || have[$k]) next
        for (i = 1; i < k; i++)
            if (tolower($i) ~ /(^|,)command=/) { print "forced"; next }
        have[$k] = 1
        print "add " $0
    }
' "$src")

added=$(grep -c '^add ' <<<"$plan")
forced=$(grep -c '^forced$' <<<"$plan")

if [ "$forced" -gt 0 ]; then
    echo "Left out $forced key(s) of root's with a forced command (command=): they open no shell."
fi
if [ "$added" -eq 0 ]; then
    echo "No key of root's was missing from $dest."
    exit 0
fi

tmp=
if tmp=$(mktemp "$dest.XXXXXX") &&
    { if [ -f "$dest" ]; then
          cat "$dest"
          # A last line without its newline would run into the first added.
          [ -z "$(tail -c 1 "$dest")" ] || echo
      fi
      sed -n 's/^add //p' <<<"$plan"; } > "$tmp" &&
    chown "$operator:" "$tmp" &&
    chmod 600 "$tmp" &&
    mv -f "$tmp" "$dest"; then
    echo "Added $added key(s) of root's to $dest."
    exit 0
fi
[ -z "$tmp" ] || rm -f "$tmp"
echo "Root's keys could not be added to $dest."
exit 1
