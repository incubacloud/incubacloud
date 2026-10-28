#!/usr/bin/env bats
# Tests for scripts/backup_download.sh.
#
# ``docker`` and ``zip`` are stubbed on PATH. What is exercised here is
# the logic that used to live in the executor's f-strings: which
# container each mode talks to, the duplicity flags, and the packaging.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/backup_download.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"
    TMPDIR_ARG="$TMP/scratch"

    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/zip" <<'STUB'
#!/usr/bin/env bash
echo "zip $*" >> "$CALLS"
STUB
    # ``compose ps --status running`` answers what RUNNING lists, a live
    # instance unless a test says otherwise; PS_FAIL makes it fail.
    # ``compose config --services`` answers what SERVICES lists;
    # CONFIG_FAIL makes it fail. A ``compose run ... bash -c <program>``
    # keeps its program in INNER, to be run here.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
    [ ! -e "$PS_FAIL" ] || exit 1
    cat "$RUNNING"
fi
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    [ ! -e "$CONFIG_FAIL" ] || exit 1
    cat "$SERVICES"
fi
if [ "$1" = "compose" ] && [ "$2" = "run" ]; then
    prev=""
    for arg in "$@"; do
        [ "$prev" = "-c" ] && printf '%s' "$arg" > "$INNER"
        prev="$arg"
    done
fi
if [ "$1" = "inspect" ]; then
    # The ids ``compose ps -q`` printed are the service names themselves.
    shift 3
    printf '%s\n' "$@"
fi
exit 0
STUB
    chmod +x "$TMP/bin/zip" "$TMP/bin/docker"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    export RUNNING="$TMP/running"
    printf 'odoo\ndb\n' > "$RUNNING"
    export PS_FAIL="$TMP/ps_fail"
    export SERVICES="$TMP/services"
    printf 'odoo\ndb\nsmtp\n' > "$SERVICES"
    export CONFIG_FAIL="$TMP/config_fail"
    export INNER="$TMP/inner"
    export PATH="$TMP/bin:$PATH"
}

# Stub, in $TMP/ctr, what the dump's wait runs inside the container:
# ``pg_isready`` exits with $1 every time, ``date +%s`` advances 30
# seconds a call from 1000, and ``sleep`` returns at once.
container_stubs() {
    mkdir -p "$TMP/ctr"
    printf '#!/usr/bin/env bash\nexit "$(cat "%s")"\n' "$TMP/pg_ready" > "$TMP/ctr/pg_isready"
    printf '#!/usr/bin/env bash\nnow="$(cat "%s")"\necho "$now"\necho $((now + 30)) > "%s"\n' \
        "$TMP/clock" "$TMP/clock" > "$TMP/ctr/date"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/ctr/sleep"
    chmod +x "$TMP/ctr/pg_isready" "$TMP/ctr/date" "$TMP/ctr/sleep"
    echo 1000 > "$TMP/clock"
    echo "$1" > "$TMP/pg_ready"
}

teardown() {
    rm -rf "$TMP"
}

@test "a db-only live dump asks for no filestore" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip db
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose run --label traefik.enable=false --label sablier.enable=false --rm --no-deps -e WAIT_DB=false -e UNACCENT=false -v /tmp:/host-tmp odoo bash -c"* ]]
    [[ "$(cat "$CALLS")" == *" live-dump click-odoo-backupdb --no-filestore prod /host-tmp/out.zip"* ]]
}

@test "a live dump mounts only the archive's own directory" {
    # The job's drop directory, not the host's /tmp: the container saw
    # every other job's staging there, and wrote the dump where any
    # account could read it.
    drop="$TMP/job/out"
    run bash "$SCRIPT" live-dump "$DIR" prod "$drop/backup.zip" all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *" -v $drop:/host-tmp odoo "* ]]
    [[ "$(cat "$CALLS")" != *"-v /tmp:/host-tmp"* ]]
    [[ "$(cat "$CALLS")" == *" /host-tmp/backup.zip"* ]]
}

@test "a live dump uses the series' tool the panel names" {
    IC_DB_TOOL=odoo-db run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *" live-dump odoo db dump prod /host-tmp/out.zip"* ]]
    [[ "$(cat "$CALLS")" != *"click-odoo"* ]]
}

@test "a live dump of an unknown scope runs nothing" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip filestore
    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown scope: filestore"* ]]
    [[ "$(cat "$CALLS")" != *"docker compose run"* ]]
}

@test "an 'all' live dump includes the filestore" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--filestore prod"* ]]
    [[ "$(cat "$CALLS")" != *"--no-filestore"* ]]
}

@test "a live dump's one-off container stays out of Traefik and Sablier" {
    # It inherits the service's labels: the proxy would send visitors to a
    # container that serves nothing while the dump runs, and Sablier would
    # hold a sleeping tenant's panel behind its waiting page.
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"run --label traefik.enable=false --label sablier.enable=false --rm "* ]]
}

@test "a live dump leaves a running database and what it needs alone" {
    # Without --no-deps Compose recreates any dependency whose definition
    # changed since the stack started: on 22-oct-2026 a nightly backup
    # recreated a staging's live db and started a gatekeeper its odoo was
    # not on.
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--rm --no-deps -e WAIT_DB=false -e UNACCENT=false -v /tmp:/host-tmp odoo "* ]]
    [[ "$(cat "$CALLS")" != *"compose up"* ]]
}

@test "a live dump of a running database waits for it, a minute at most" {
    # Doodba's own wait has no deadline: a database that runs but does
    # not answer would hold the dump until the SSH timeout.
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    grep -q 'until pg_isready -q -t 5' "$INNER"
    grep -q 'deadline=\$((\$(date +%s) + 60))' "$INNER"
    container_stubs 2
    run env PATH="$TMP/ctr:$PATH" bash -c "$(cat "$INNER")" live-dump echo dumped
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR: the running database did not answer within 60s"* ]]
    [[ "$output" != *"dumped"* ]]
}

@test "a running database that answers is dumped by the wait's exec" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    container_stubs 0
    run env PATH="$TMP/ctr:$PATH" bash -c "$(cat "$INNER")" live-dump echo dumped prod
    [ "$status" -eq 0 ]
    [ "$output" = "dumped prod" ]
}

@test "a live dump of a stopped database starts it, as before" {
    # A move dumps the source of a panel Sablier put to sleep; what it
    # depends on is started with it, a gatekeeper included, and doodba's
    # own wait for Postgres is the one that waits.
    printf 'smtp\n' > "$RUNNING"
    printf 'odoo\ndb\ngatekeeper\n' > "$SERVICES"
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$output" == *"the database is not running: the dump starts it"* ]]
    [[ "$(cat "$CALLS")" == *"--rm -v /tmp:/host-tmp odoo click-odoo-backupdb "* ]]
    [[ "$(cat "$CALLS")" != *"--no-deps"* ]]
    [[ "$(cat "$CALLS")" != *"WAIT_DB"* ]]
    [[ "$(cat "$CALLS")" != *"compose config"* ]]
    [ ! -s "$INNER" ]
}

@test "a dump asked for a running database refuses a stopped one" {
    : > "$RUNNING"
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all running
    # Its own code, so the refusal can be told from a failed dump.
    [ "$status" -eq 25 ]
    [[ "$output" == *"ERROR: the database of $DIR is not running: a live dump does not start it"* ]]
    [[ "$(cat "$CALLS")" != *"compose run"* ]]
}

@test "a dump asked for a running database takes a running one" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all running
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--rm --no-deps -e WAIT_DB=false -e UNACCENT=false -v /tmp:/host-tmp odoo "* ]]
}

@test "a live dump does not start a gatekeeper that is not running" {
    # 22-oct-2026: a staging whose files template v10 rewrote, never
    # rebuilt: its odoo is declared in a gatekeeper's namespace that
    # does not run, and the one-off would fail in docker's words.
    printf 'odoo\ndb\ngatekeeper\n' > "$SERVICES"
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all running
    [ "$status" -eq 1 ]
    [[ "$output" == *"the gatekeeper of $DIR is not running (not rebuilt under template v10?): a live dump does not start it"* ]]
    [[ "$(cat "$CALLS")" != *"compose run"* ]]
}

@test "a live dump runs in a gatekeeper that runs" {
    printf 'odoo\ndb\ngatekeeper\n' > "$SERVICES"
    printf 'odoo\ndb\ngatekeeper\n' > "$RUNNING"
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--rm --no-deps -e WAIT_DB=false -e UNACCENT=false -v /tmp:/host-tmp odoo "* ]]
}

@test "a live dump that cannot read the compose runs nothing" {
    touch "$CONFIG_FAIL"
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 1 ]
    [[ "$output" == *"could not read the compose of $DIR"* ]]
    [[ "$(cat "$CALLS")" != *"compose run"* ]]
}

@test "a live dump that cannot read what runs runs nothing" {
    touch "$PS_FAIL"
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 1 ]
    [[ "$output" == *"could not read which services are running"* ]]
    [[ "$(cat "$CALLS")" != *"compose run"* ]]
}

@test "an unknown live-dump condition is refused" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all awake
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown condition: awake"* ]]
    [[ "$(cat "$CALLS")" != *"compose run"* ]]
}

@test "a live dump never touches the backup container" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip db
    [[ "$(cat "$CALLS")" != *"exec -T backup"* ]]
    [[ "$(cat "$CALLS")" != *"dup restore"* ]]
}

@test "an unknown live-dump scope is refused" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip sideways
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown scope"* ]]
}

@test "restore-sql pulls a single dump out of duplicity" {
    run bash "$SCRIPT" restore-sql "$DIR" prod latest
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose exec -T backup sh -c"* ]]
    [[ "$(cat "$CALLS")" == *"dup restore --force"* ]]
    [[ "$(cat "$CALLS")" == *"--path-to-restore prod.sql"* ]]
    [[ "$(cat "$CALLS")" == *'"$DST"'* ]]
}

@test "restore-full restores the whole tree, unfiltered" {
    run bash "$SCRIPT" restore-full "$DIR" latest
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"dup restore --force"* ]]
    [[ "$(cat "$CALLS")" != *"--path-to-restore"* ]]
}

@test "'latest' omits the --time flag" {
    run bash "$SCRIPT" restore-sql "$DIR" prod latest
    [[ "$(cat "$CALLS")" != *"--time"* ]]
}

@test "an explicit timestamp becomes a --time flag" {
    run bash "$SCRIPT" restore-sql "$DIR" prod 2026-03-19T02:00:00
    [[ "$(cat "$CALLS")" == *"--time 2026-03-19T02:00:00"* ]]
}

@test "package-sql copies the dump out and zips it" {
    run bash "$SCRIPT" package-sql "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose cp backup:/tmp/bkdl/prod.sql $TMPDIR_ARG/dump.sql"* ]]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql"* ]]
    [ ! -d "$TMPDIR_ARG" ]
}

@test "package-sql cleans the container scratch dir" {
    run bash "$SCRIPT" package-sql "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [[ "$(cat "$CALLS")" == *"docker compose exec -T backup rm -rf /tmp/bkdl"* ]]
}

@test "package-full renames the dump and adds the filestore" {
    # Stand in for what `docker compose cp` would have dropped there.
    mkdir -p "$TMPDIR_ARG/odoo/filestore/prod"
    : > "$TMPDIR_ARG/prod.sql"
    run bash "$SCRIPT" package-full "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose cp backup:/tmp/bkdl/. $TMPDIR_ARG/"* ]]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql filestore"* ]]
    [ ! -d "$TMPDIR_ARG" ]
}

@test "package-full survives an instance with no filestore" {
    # The filestore copy is best-effort: an instance that has none must
    # still produce an archive with the dump in it.
    mkdir -p "$TMPDIR_ARG"
    : > "$TMPDIR_ARG/prod.sql"
    run bash "$SCRIPT" package-full "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql filestore"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    run bash "$SCRIPT" restore-sql "~/projects/inst" prod latest
    [ "$status" -eq 0 ]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" restore-sql "$HOME/projects/gone" prod latest
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR" prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}

# ── handover: the archive a chained restore picks up ─────────────────

@test "a handover moves the archive into a new directory of its own, 0700" {
    mkdir -p "$TMP/job/out"
    printf 'PK' > "$TMP/job/out/backup.zip"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-x"
    [ "$status" -eq 0 ]
    [ "$(stat -c %a "$TMP/handoff-x")" = 700 ]
    [ "$(cat "$TMP/handoff-x/archive.zip")" = PK ]
    [ ! -e "$TMP/job/out/backup.zip" ]
}

@test "a handover refuses a link where the archive should be" {
    mkdir -p "$TMP/job/out"
    printf 'key' > "$TMP/secret"
    ln -s "$TMP/secret" "$TMP/job/out/backup.zip"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-x"
    [ "$status" -ne 0 ]
    [ ! -e "$TMP/handoff-x" ]
}

@test "a handover refuses a path something else holds" {
    mkdir -p "$TMP/job/out"
    printf 'PK' > "$TMP/job/out/backup.zip"
    ln -s "$TMP" "$TMP/handoff-x"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-x"
    [ "$status" -ne 0 ]
    [ -L "$TMP/handoff-x" ]
    [ -e "$TMP/job/out/backup.zip" ]
    rm -f "$TMP/handoff-x"
    printf 'other' > "$TMP/handoff-x"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-x"
    [ "$status" -ne 0 ]
    [ "$(cat "$TMP/handoff-x")" = other ]
}

@test "a handover replaces what an earlier attempt of the job left" {
    mkdir -p "$TMP/job/out" "$TMP/handoff-x"
    printf 'old' > "$TMP/handoff-x/archive.zip"
    printf 'PK' > "$TMP/job/out/backup.zip"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-x"
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/handoff-x/archive.zip")" = PK ]
    [ "$(stat -c %a "$TMP/handoff-x")" = 700 ]
}

@test "a handover never prints where the handoff is" {
    # Named with the panel's secret so no other account knows it in
    # advance; the job log is read by others than the host's account.
    mkdir -p "$TMP/job/out"
    printf 'PK' > "$TMP/job/out/backup.zip"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-secretname"
    [ "$status" -eq 0 ]
    [[ "$output" != *secretname* ]]
    printf 'PK' > "$TMP/job/out/backup.zip"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-secretname"
    [ "$status" -eq 0 ]
    [[ "$output" != *secretname* ]]
    rm -rf "$TMP/handoff-secretname"
    ln -s "$TMP" "$TMP/handoff-secretname"
    run bash "$SCRIPT" handover "$DIR" "$TMP/job/out/backup.zip" "$TMP/handoff-secretname"
    [ "$status" -ne 0 ]
    [[ "$output" != *secretname* ]]
}
