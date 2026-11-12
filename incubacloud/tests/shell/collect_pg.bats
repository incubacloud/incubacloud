#!/usr/bin/env bats
# Tests for ansible/playbooks/files/collect_pg.sh.
#
# ``docker`` and ``psql`` are stubbed. The docker stub answers from
# STUB_PS (``docker ps -q``) and STUB_INSPECT (``docker inspect``), or
# fails as ``timeout`` does for the command named in STUB_DOCKER_HANGS; the psql
# stub records its arguments and environment, and answers STUB_ROW, sleeps
# STUB_SLEEP or fails when the socket it is given is listed in STUB_FAIL.
# /proc is a directory of this test (IC_PG_PROC), the lock one of its files.

bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../ansible/playbooks/files/collect_pg.sh"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/bin" "$TMP/textfile" "$TMP/proc/101/root/run/postgresql" \
        "$TMP/proc/102/root/var/run/postgresql"
    export TMP
    export IC_PG_PROC="$TMP/proc"
    export IC_PG_LOCK="$TMP/lock"
    export STUB_PS=$'aaa\nbbb'
    export STUB_INSPECT=$'acme-prod 101 true false\nbeta-prod 102 true false'
    export STUB_ROW="7 100 2 1 0 12 0 3 4096 0.991234"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
[ "$1" = "${STUB_DOCKER_HANGS:-}" ] && exit 124
case "$1" in
    ps) printf '%s\n' "$STUB_PS" ;;
    inspect) printf '%s\n' "$STUB_INSPECT" ;;
esac
STUB
    cat > "$TMP/bin/psql" <<'STUB'
#!/usr/bin/env bash
{
    printf 'ARGS %s\n' "$*"
    printf 'ENV %s|%s|%s\n' "$PGCONNECT_TIMEOUT" "$PGOPTIONS" "$PGAPPNAME"
} >> "$TMP/psql_calls"
sock=""
while [ "$#" -gt 0 ]; do
    [ "$1" = -h ] && sock="$2"
    shift
done
case " ${STUB_FAIL:-} " in *" $sock "*) exit 2 ;; esac
[ -n "${STUB_SLEEP:-}" ] && sleep "$STUB_SLEEP"
printf '%s\n' "$STUB_ROW"
STUB
    chmod +x "$TMP/bin/docker" "$TMP/bin/psql"
    export PATH="$TMP/bin:$PATH"
    printf '%s\n' \
        "# instance_id instance_name project dbname dbuser" \
        "11 acme acme-prod prod odoo" \
        "12 beta beta-prod main beta_user" > "$TMP/instances"
}

teardown() {
    rm -rf "$TMP"
}

collect() {
    run bash "$SCRIPT" "$TMP/instances" "$TMP/textfile"
}

@test "every running database gets its gauges, labelled by instance" {
    collect
    [ "$status" -eq 0 ]
    out="$TMP/textfile/ic_pg.prom"
    grep -qx 'ic_pg_connections{instance_id="11",instance_name="acme"} 7' "$out"
    grep -qx 'ic_pg_waiting_lock{instance_id="12",instance_name="beta"} 1' "$out"
    grep -qx 'ic_pg_cache_hit_ratio{instance_id="11",instance_name="acme"} 0.991234' "$out"
    grep -qx 'ic_pg_scrape_errors 0' "$out"
    grep -qx 'ic_pg_scrape_skipped 0' "$out"
    grep -q '^ic_pg_scrape_duration_seconds [0-9.]*$' "$out"
}

@test "each metric is one family: its samples follow its TYPE line" {
    collect
    out="$TMP/textfile/ic_pg.prom"
    [ "$(grep -c '^# TYPE ic_pg_connections gauge$' "$out")" -eq 1 ]
    [ "$(grep -c '^# TYPE ic_pg_deadlocks_total counter$' "$out")" -eq 1 ]
    run awk '/^# TYPE/ {fam=$3; next} /^#/ {next} {split($0, a, "[{ ]"); if (a[1] != fam) bad=1} END {exit bad}' "$out"
    [ "$status" -eq 0 ]
}

@test "the file is readable by node_exporter and replaced whole" {
    collect
    [ "$(stat -c %a "$TMP/textfile/ic_pg.prom")" = 644 ]
    [ -z "$(find "$TMP/textfile" -name '.ic_pg.prom.*')" ]
}

@test "each database is read through its container's own socket, as its user" {
    collect
    grep -q -- "-h $TMP/proc/101/root/run/postgresql -U odoo -d prod" "$TMP/psql_calls"
    # An image whose socket lives under /var/run only.
    grep -q -- "-h $TMP/proc/102/root/var/run/postgresql -U beta_user -d main" "$TMP/psql_calls"
}

@test "the session is read-only, and gives up on connecting and on answering" {
    collect
    grep -qx 'ENV 3|-c statement_timeout=3000 -c default_transaction_read_only=on|incubacloud-collect-pg' \
        "$TMP/psql_calls"
    run ! grep -q -- "--password\|-W" "$TMP/psql_calls"
}

@test "a stopped or sleeping database is left out and is no error" {
    export STUB_INSPECT=$'acme-prod 101 true false\nbeta-prod 0 false false'
    collect
    [ "$status" -eq 0 ]
    out="$TMP/textfile/ic_pg.prom"
    grep -q 'instance_id="11"' "$out"
    run ! grep -q 'instance_id="12"' "$out"
    grep -qx 'ic_pg_scrape_errors 0' "$out"
    [ "$(grep -c '^ARGS' "$TMP/psql_calls")" -eq 1 ]
}

@test "a database whose container is gone altogether is no error either" {
    export STUB_PS="aaa"
    export STUB_INSPECT="acme-prod 101 true false"
    collect
    grep -qx 'ic_pg_scrape_errors 0' "$TMP/textfile/ic_pg.prom"
}

@test "docker not answering counts every instance as an error, not as asleep" {
    for hangs in ps inspect; do
        export STUB_DOCKER_HANGS="$hangs"
        rm -f "$TMP/psql_calls"
        collect
        [ "$status" -eq 0 ]
        grep -qx 'ic_pg_scrape_errors 2' "$TMP/textfile/ic_pg.prom"
        [ ! -e "$TMP/psql_calls" ]
    done
}

@test "a paused database is left out without asking it" {
    export STUB_INSPECT=$'acme-prod 101 true false\nbeta-prod 102 true true'
    collect
    [ "$(grep -c '^ARGS' "$TMP/psql_calls")" -eq 1 ]
    grep -qx 'ic_pg_scrape_errors 0' "$TMP/textfile/ic_pg.prom"
}

@test "a running database that does not answer is an error, the others still count" {
    export STUB_FAIL="$TMP/proc/101/root/run/postgresql"
    collect
    [ "$status" -eq 0 ]
    out="$TMP/textfile/ic_pg.prom"
    run ! grep -q 'instance_id="11"' "$out"
    grep -q 'ic_pg_connections{instance_id="12"' "$out"
    grep -qx 'ic_pg_scrape_errors 1' "$out"
}

@test "an answer that is not ten numbers is an error, not a sample" {
    export STUB_ROW="psql: warning"
    collect
    grep -qx 'ic_pg_scrape_errors 2' "$TMP/textfile/ic_pg.prom"
    run ! grep -q '^ic_pg_connections{' "$TMP/textfile/ic_pg.prom"
}

@test "databases that hang cannot hold the pass past its budget" {
    export IC_PG_BUDGET=2 IC_PG_TIMEOUT=1 STUB_SLEEP=5
    printf '%s\n' "13 g1 beta-prod main u" "14 g2 beta-prod main u" "15 g3 beta-prod main u" \
        >> "$TMP/instances"
    start=$SECONDS
    collect
    [ "$status" -eq 0 ]
    [ $((SECONDS - start)) -le 4 ]
    out="$TMP/textfile/ic_pg.prom"
    errors=$(awk '$1 == "ic_pg_scrape_errors" {print $2}' "$out")
    skipped=$(awk '$1 == "ic_pg_scrape_skipped" {print $2}' "$out")
    [ "$skipped" -ge 1 ]
    [ $((errors + skipped)) -eq 5 ]
}

@test "a pass still running makes the next one exit without writing" {
    exec 8>"$IC_PG_LOCK"
    flock -n 8
    collect
    [ "$status" -eq 0 ]
    [ ! -e "$TMP/textfile/ic_pg.prom" ]
    [ ! -e "$TMP/psql_calls" ]
    exec 8>&-
}

@test "agents removed: no directory, nothing written and no error" {
    rm -rf "$TMP/textfile"
    collect
    [ "$status" -eq 0 ]
    [ ! -e "$TMP/textfile" ]
    [ ! -e "$TMP/psql_calls" ]
}

@test "a directory that is a link is not written through" {
    mkdir "$TMP/elsewhere"
    rm -rf "$TMP/textfile"
    ln -s "$TMP/elsewhere" "$TMP/textfile"
    collect
    [ "$status" -eq 0 ]
    [ -z "$(ls -A "$TMP/elsewhere")" ]
}

@test "no instance yet still writes the pass's own figures" {
    : > "$TMP/instances"
    collect
    [ "$status" -eq 0 ]
    grep -qx 'ic_pg_scrape_errors 0' "$TMP/textfile/ic_pg.prom"
}

@test "label values are escaped" {
    printf '%s\n' '16 we"ird beta-prod main u' > "$TMP/instances"
    collect
    grep -qF 'instance_name="we\"ird"' "$TMP/textfile/ic_pg.prom"
}
