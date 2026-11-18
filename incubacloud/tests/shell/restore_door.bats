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

# take <granted> <received> <local>: run the shell a restore takes an
# upload with (``restore_instance_executor._take_upload``), built by the
# panel's own code: its two functions, read out of the module.
take() {
    local built
    built="$(python3 - "${BATS_TEST_DIRNAME}/../../models/restore_instance_executor.py" "$@" <<'PY'
import ast, posixpath, shlex, sys
path = sys.argv[1]
names = ("_where_its_name_says", "_take_upload")
tree = ast.parse(open(path, encoding="utf-8").read())
body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
assert len(body) == len(names), "not found in %s" % path
space = {"posixpath": posixpath, "shlex": shlex}
exec(compile(ast.Module(body=body, type_ignores=[]), path, "exec"), space)
print(space["_take_upload"](*sys.argv[2:]))
PY
)"
    bash -c "$built"
}

# held <files...>: start an upload of <files> whose session is held open
# after the door's check, before rsync reads what the client sends, until
# $TMP/go exists; its exit status lands in $TMP/held.status.
held() {
    cat > "$TMP/slow-rrsync" <<STUB
#!/bin/sh
touch "$TMP/waiting"
while [ ! -e "$TMP/go" ]; do sleep 0.1; done
exec /usr/bin/rrsync "\$@"
STUB
    chmod +x "$TMP/slow-rrsync"
    cat > "$TMP/ssh-held" <<STUB
#!/usr/bin/env bash
shift
export SSH_ORIGINAL_COMMAND="\$*"
exec /bin/bash -c 'ulimit -f '"\$LIMIT_KB"' && exec /bin/bash $TMP/root/.door.sh receive $TMP/slow-rrsync -wo -munge -no-del $DOOR'
STUB
    chmod +x "$TMP/ssh-held"
    # Its status kept whatever it is: bats runs a test under set -e.
    { st=0
      rsync -P --inplace -e "$TMP/ssh-held" "$@" h:restore.zip >/dev/null 2>&1 || st=$?
      echo "$st" > "$TMP/held.st"; mv "$TMP/held.st" "$TMP/held.status"; } 3>&- &
    for _ in $(seq 100); do [ -e "$TMP/waiting" ] && break; sleep 0.1; done
    [ -e "$TMP/waiting" ]
}

# release: let the held session go on, and wait for it to end.
release() {
    touch "$TMP/go"
    for _ in $(seq 300); do [ -e "$TMP/held.status" ] && break; sleep 0.1; done
    [ -e "$TMP/held.status" ]
}

@test "install copies the command in one rename, read and run only" {
    [ -x "$TMP/root/.door.sh" ]
    [ "$(stat -c %a "$TMP/root/.door.sh")" = 500 ]
    cmp "$SCRIPT" "$TMP/root/.door.sh"
    ! ls "$TMP/root" | grep -q '\.new\.'
}

@test "install goes on past what an earlier one left, and leaves nothing" {
    for pid in 1 $$ 99999; do
        : > "$TMP/root/.door.sh.new.$pid"
        chmod 500 "$TMP/root/.door.sh.new.$pid"
    done
    run bash "$SCRIPT" install "$TMP/root/.door.sh"
    [ "$status" -eq 0 ]
    cmp "$SCRIPT" "$TMP/root/.door.sh"
    [ "$(ls -A "$TMP/root" | grep -c '\.new\.')" -eq 3 ]
}

@test "the upload the dialog gives goes on to the wrapper" {
    receive_line "rsync --server -e.LsfxCIvu --log-format=X --inplace . restore.zip"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/ran")" = "-wo -munge -no-del $DOOR" ]
    # Created empty first, so a second file finds a file there.
    [ -f "$DOOR/restore.zip" ]
    [ ! -s "$DOOR/restore.zip" ]
}

@test "append and -v, -t and -z are taken too" {
    receive_line "rsync --server -vvtze.iLsfxCIvu --bwlimit=1000 --append --append . restore.zip"
    [ "$status" -eq 0 ]
    [ -e "$TMP/ran" ]
}

@test "metadata, devices, owners, groups and modes are refused" {
    # -X and -A store what neither ulimit -f nor du counts, -D makes a
    # device or a pipe, -o, -g and -p hold where the panel is root.
    for option in X A pA D o g p vtzX logDtp; do
        rm -f "$TMP/ran"
        receive_line "rsync --server -${option}e.LsfxCIvu --log-format=X --inplace . restore.zip"
        [ "$status" -eq 1 ] || { echo "taken: -$option"; false; }
        [ ! -e "$TMP/ran" ] || { echo "ran: -$option"; false; }
        [[ "$output" == *"is not taken (only -v, -t and -z"* ]]
        [ -z "$(ls -A "$DOOR")" ] || { echo "wrote for: -$option"; false; }
    done
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
    [[ "$output" == *"the option -logDpr is not taken"* ]]
    receive_line "rsync --server -vre.iLsfxCIvu --inplace . restore.zip"
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

@test "lab: with -v, -t and -z as people add them, it arrives whole" {
    real_rsync
    run rsync -vztP --inplace -e "$TMP/ssh" "$TMP/small.zip" h:restore.zip
    [ "$status" -eq 0 ]
    cmp "$TMP/small.zip" "$DOOR/restore.zip"
}

@test "lab: -X, -A, -D, -o, -g and -p are refused by real rsync too" {
    real_rsync
    for option in -X -A -D -o -g -p; do
        run rsync -P --inplace "$option" -e "$TMP/ssh" "$TMP/small.zip" h:restore.zip
        [ "$status" -ne 0 ] || { echo "taken: $option"; false; }
        [ -z "$(ls -A "$DOOR")" ] || { echo "wrote for: $option"; false; }
    done
}

@test "lab: a session held open since before the take cannot send a second file" {
    # The restore takes the archive while a session of the key, opened
    # before, has not yet told rsync what it sends: the door's name is
    # never free (an empty file takes it), so rsync refuses a second file
    # rather than make a directory of the name for any number of them.
    real_rsync
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/small.zip" h:restore.zip
    [ "$status" -eq 0 ]
    held "$TMP/small.zip" "$TMP/other.zip"
    mkdir -m 700 "$TMP/job"
    take "$DOOR/restore.zip" "$TMP/job/upload.received" "$TMP/job/restore.zip"
    cmp "$TMP/small.zip" "$TMP/job/restore.zip"
    [ "$(ls -A "$TMP/job")" = restore.zip ]
    release
    [ "$(cat "$TMP/held.status")" -ne 0 ]
    [ "$(ls -A "$DOOR")" = restore.zip ]
    [ -f "$DOOR/restore.zip" ] && [ ! -L "$DOOR/restore.zip" ]
    [ ! -s "$DOOR/restore.zip" ]
}

@test "lab: with the name free after the take, that session filled a directory" {
    # Why the take never leaves it free: the archive moved away as
    # before 1.0.239, the held session's two files both arrive.
    real_rsync
    run rsync -P --inplace -e "$TMP/ssh" "$TMP/small.zip" h:restore.zip
    [ "$status" -eq 0 ]
    held "$TMP/small.zip" "$TMP/other.zip"
    mv -fT -- "$DOOR/restore.zip" "$TMP/moved.zip"
    release
    [ "$(cat "$TMP/held.status")" -eq 0 ]
    [ -f "$DOOR/restore.zip/small.zip" ]
    [ -f "$DOOR/restore.zip/other.zip" ]
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

@test "lab: rrsync alone, the command before 1.0.239, took any number of files" {
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
