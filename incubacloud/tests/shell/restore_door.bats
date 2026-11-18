#!/usr/bin/env bats
# Tests for scripts/restore_door.sh: what an upload door's key runs.
#
# Uploads go through the real rsync and the real rrsync when the machine
# has them (``rsync -e`` runs a stand-in for ssh that sets
# SSH_ORIGINAL_COMMAND and runs the key's forced command, as sshd does),
# under a real ``ulimit -f``. The checks of the command line itself need
# neither, with a wrapper that records what it was asked.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/restore_door.sh"
    TMP="$(mktemp -d)"
    DOOR="$TMP/root/door"
    mkdir -p "$DOOR"
    chmod 700 "$TMP/root"
    # The door's command, installed as the job that opens a door does.
    bash "$SCRIPT" install "$TMP/root/.door.sh"
    # 1 MiB, the key's limit in KiB as ``forced_command`` sets it.
    export LIMIT_KB=1024
    # ssh, as sshd runs the key's forced command: the host name dropped,
    # the rest of the line in SSH_ORIGINAL_COMMAND.
    cat > "$TMP/ssh" <<STUB
#!/usr/bin/env bash
shift
export SSH_ORIGINAL_COMMAND="\$*"
exec /bin/bash -c 'ulimit -f '"\$LIMIT_KB"' && exec /bin/bash $TMP/root/.door.sh receive /usr/bin/rrsync -wo -munge -no-del $DOOR'
STUB
    chmod +x "$TMP/ssh"
    # A wrapper that only records it ran, for the checks of the line.
    printf '#!/bin/sh\necho "$@" > "%s/ran"\n' "$TMP" > "$TMP/record"
    chmod +x "$TMP/record"
    head -c 4096 /dev/urandom > "$TMP/small.zip"
    head -c 2048 /dev/urandom > "$TMP/other.zip"
}

teardown() {
    rm -rf "$TMP"
}

# receive_line <line>: run the door's command for a client that sent
# <line>, with the recording wrapper.
receive_line() {
    SSH_ORIGINAL_COMMAND="$1" run bash "$TMP/root/.door.sh" receive \
        "$TMP/record" -wo -munge -no-del "$DOOR"
}

real_rsync() {
    [ -x /usr/bin/rrsync ] && command -v rsync >/dev/null \
        || skip "no rsync and rrsync here"
}

@test "install copies the command in one rename, read and run only" {
    [ -x "$TMP/root/.door.sh" ]
    [ "$(stat -c %a "$TMP/root/.door.sh")" = 500 ]
    cmp "$SCRIPT" "$TMP/root/.door.sh"
    ! ls "$TMP/root" | grep -q '\.new\.'
}

@test "the upload the dialog gives goes on to the wrapper" {
    receive_line "rsync --server -e.LsfxCIvu --log-format=X --inplace . restore.zip"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/ran")" = "-wo -munge -no-del $DOOR" ]
    # Created empty first, so a second file finds a file there.
    [ -f "$DOOR/restore.zip" ]
    [ ! -s "$DOOR/restore.zip" ]
}

@test "append and a few harmless options are taken too" {
    receive_line "rsync --server -vlogDtpze.iLsfxCIvu --bwlimit=1000 --append --append . restore.zip"
    [ "$status" -eq 0 ]
    [ -e "$TMP/ran" ]
}

@test "every other shape of line is refused before the wrapper runs" {
    for line in \
        "rsync --server -vlogDtpre.iLsfxCIvu --inplace . restore.zip" \
        "rsync --server -re.iLsfxCIvu --inplace . restore.zip" \
        "rsync --server -de.iLsfxCIvu --inplace . restore.zip" \
        "rsync --server -Re.LsfxCIvu --inplace . restore.zip" \
        "rsync --server -be.LsfxCIvu --inplace . restore.zip" \
        "rsync --server -se.LsfxCIvu" \
        "rsync --server -e.LsfxCIvu --log-format=X . restore.zip" \
        "rsync --server -e.LsfxCIvu --inplace . other.zip" \
        "rsync --server -e.LsfxCIvu --inplace . restore.zip/" \
        "rsync --server -e.LsfxCIvu --inplace . ." \
        "rsync --server -e.LsfxCIvu --inplace . restore.zip extra.zip" \
        "rsync --server -e.LsfxCIvu --inplace --backup-dir=x . restore.zip" \
        "rsync --server -e.LsfxCIvu --inplace --partial-dir=x . restore.zip" \
        "rsync --server --sender -e.LsfxCIvu --inplace . restore.zip" \
        "rsync --server -e.LsfxCIvu --inplace  . restore.zip" \
        "rsync --server -e.LsfxCIvu --inplace . restore.zip;id" \
        'rsync --server -e.LsfxCIvu --inplace . restore\ .zip' \
        "sh -c id" \
        ""; do
        rm -f "$TMP/ran"
        receive_line "$line"
        [ "$status" -eq 1 ] || { echo "taken: $line"; false; }
        [ ! -e "$TMP/ran" ] || { echo "ran: $line"; false; }
        [[ "$output" == *"This upload door takes one file, sent as restore.zip"* ]]
        [ -z "$(ls -A "$DOOR")" ] || { echo "wrote for: $line"; false; }
    done
}

@test "a refusal says which option it does not take" {
    receive_line "rsync --server -vlogDtpre.iLsfxCIvu --inplace . restore.zip"
    [[ "$output" == *"the option -r is not taken"* ]]
    receive_line "rsync --server -e.LsfxCIvu --inplace --delete . restore.zip"
    [[ "$output" == *"the option --delete is not taken"* ]]
}

@test "lab: one archive within the limit arrives whole" {
    real_rsync
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/small.zip" h:restore.zip
    [ "$status" -eq 0 ]
    cmp "$TMP/small.zip" "$DOOR/restore.zip"
    [ "$(ls -A "$DOOR")" = restore.zip ]
}

@test "lab: a second file in one session is refused" {
    real_rsync
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/small.zip" "$TMP/other.zip" h:restore.zip
    [ "$status" -ne 0 ]
    [ "$(ls -A "$DOOR")" = restore.zip ]
    [ ! -s "$DOOR/restore.zip" ]
}

@test "lab: another name, or the door's directory, is refused" {
    real_rsync
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/other.zip" h:other.zip
    [ "$status" -ne 0 ]
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/other.zip" h:
    [ "$status" -ne 0 ]
    [ -z "$(ls -A "$DOOR")" ]
}

@test "lab: a directory sent with -a is refused" {
    real_rsync
    mkdir -p "$TMP/tree"
    cp "$TMP/small.zip" "$TMP/other.zip" "$TMP/tree/"
    run rsync -aP --inplace -e "$TMP/ssh" "$TMP/tree/" h:restore.zip
    [ "$status" -ne 0 ]
    [ -z "$(ls -A "$DOOR")" ]
}

@test "lab: a file past the limit stops at it, and so does all the door holds" {
    real_rsync
    head -c $(( 3 * 1024 * 1024 )) /dev/urandom > "$TMP/large.zip"
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/large.zip" h:restore.zip
    [ "$status" -ne 0 ]
    [ "$(stat -c %s "$DOOR/restore.zip")" -le $(( LIMIT_KB * 1024 )) ]
    # A second session writes the same file again, never one beside it.
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/large.zip" h:restore.zip
    [ "$(ls -A "$DOOR")" = restore.zip ]
    [ "$(du -sb "$DOOR" | cut -f1)" -le $(( LIMIT_KB * 1024 + 4096 )) ]
}

@test "lab: without --inplace nothing is sent" {
    real_rsync
    run rsync -P -e "$TMP/ssh" "$TMP/small.zip" h:restore.zip
    [ "$status" -ne 0 ]
    [ -z "$(ls -A "$DOOR")" ]
}

@test "lab: rrsync alone, the command before 1.0.238, took any number of files" {
    # Why the door checks the line: each file was held to the limit, and
    # the files together to nothing but the watch every five minutes.
    real_rsync
    cat > "$TMP/ssh-old" <<STUB
#!/usr/bin/env bash
shift
export SSH_ORIGINAL_COMMAND="\$*"
exec /bin/bash -c 'ulimit -f '"\$LIMIT_KB"' && exec /usr/bin/rrsync -wo -munge -no-del $DOOR'
STUB
    chmod +x "$TMP/ssh-old"
    run rsync -P --inplace -e "$TMP/ssh-old" "$TMP/small.zip" "$TMP/other.zip" h:restore.zip
    [ "$status" -eq 0 ]
    [ -f "$DOOR/restore.zip/small.zip" ]
    [ -f "$DOOR/restore.zip/other.zip" ]
}
