#!/usr/bin/env bats
# Tests for scripts/rebuild.sh. git/copier/docker/sleep are stubbed;
# resolve-conflicts runs on real files.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/rebuild.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"

    mkdir -p "$TMP/bin" "$HOME/.local/bin"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    for tool in git docker sleep; do
        cat > "$TMP/bin/$tool" <<STUB
#!/usr/bin/env bash
echo "$tool \$*" >> "\$CALLS"
STUB
        chmod +x "$TMP/bin/$tool"
    done
    # What the boot test asks before it copies, answered the same in every
    # docker stub below (each sources this file after recording the call):
    # the database's container, its data directory and hba file, its size
    # (1 GiB unless STUB_DB_KB says) and the free space of Docker's disk
    # (100 GiB unless STUB_FREE_KB says). With STUB_HBA_FILE the hba file
    # is that one, and the shell the test runs in the database's container
    # to change it runs here, on it, its temporary file under $TMP; so does
    # the database's reload, through the psql stub below, which keeps the
    # file as it was when reloaded in $TMP/hba.loaded (what the database
    # holds in memory). STUB_COPY_BLOCKS makes the copy say it started, in $TMP/copying, and
    # last until it is killed.
    export TMP
    export BOOT_ANSWERS="$TMP/boot-answers"
    cat > "$BOOT_ANSWERS" <<'STUB'
case "$*" in
    "compose ps -q db")          echo db123; exit 0 ;;
    *"SHOW data_directory"*)     echo /var/lib/postgresql/data; exit 0 ;;
    *"SHOW hba_file"*)           echo "${STUB_HBA_FILE:-/etc/postgres/pg_hba.conf}"; exit 0 ;;
    "compose exec -T db sh -c "*)
        if [ -n "${STUB_HBA_FILE:-}" ]; then
            shift 6
            body="${1//\/tmp\/ic_boot_hba/$TMP/ic_boot_hba}"
            shift
            exec sh -c "$body" "$@"
        fi ;;
    "compose exec -T db psql "*"pg_reload_conf"*)
        if [ -n "${STUB_HBA_FILE:-}" ]; then
            shift 4
            exec "$@"
        fi ;;
    *"pg_basebackup"*)
        if [ -n "${STUB_COPY_BLOCKS:-}" ]; then
            : > "$TMP/copying"
            exec /bin/sleep 30
        fi ;;
    *"du -sk"*)                  printf '%s\t/var/lib/postgresql/data\n' "${STUB_DB_KB:-1048576}"; exit 0 ;;
    *"df -Pk /out"*)             echo "${STUB_FREE_KB:-104857600}"; exit 0 ;;
    *"State.OOMKilled"*)         echo "${STUB_COPY_OOM:-false}"; exit 0 ;;
esac
STUB
    # The boot test asks docker two questions it cannot proceed without:
    # which services were running, so it can put them back, and the exit
    # code of the boot container, which it reads with ``docker wait``. A
    # stub that answers nothing makes the step fail, which is correct
    # behaviour and not what these tests exercise.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
. "$BOOT_ANSWERS"
case "$*" in
    "wait "*)                     echo 0 ;;
    *"compose config --services"*)    printf 'odoo\ndb\nsmtp\n' ;;
    *"compose ps -q --status running"*)    printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*)    shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    cat > "$TMP/bin/psql" <<'STUB'
#!/usr/bin/env bash
echo "psql $*" >> "$CALLS"
[ -z "${STUB_HBA_FILE:-}" ] || cp "$STUB_HBA_FILE" "$TMP/hba.loaded"
STUB
    chmod +x "$TMP/bin/psql"
    # The script invokes copier by absolute path (~/.local/bin/copier).
    cat > "$HOME/.local/bin/copier" <<STUB
#!/usr/bin/env bash
echo "copier \$*" >> "\$CALLS"
STUB
    chmod +x "$HOME/.local/bin/copier"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "copier-update exports the pipx PATH and runs copier update" {
    run bash "$SCRIPT" copier-update "$DIR" /tmp/answers.yml
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"copier update --defaults --trust --data-file /tmp/answers.yml $DIR"* ]]
}

@test "copier-update reads the git default branch before writing it" {
    # Read-first guard: an unconditional write races the ~/.gitconfig lock
    # when sibling jobs target the same host. Already set here,
    # so no write must follow.
    cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$*" in
    *"--get init.defaultBranch"*) echo "master"; exit 0 ;;
esac
STUB
    chmod +x "$TMP/bin/git"
    run bash "$SCRIPT" copier-update "$DIR" /tmp/answers.yml
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"git config --global --get init.defaultBranch"* ]]
    [[ "$(cat "$CALLS")" != *"git config --global init.defaultBranch master"* ]]
}

@test "commit-dirty stages and commits with an inline identity" {
    # git diff --cached --quiet must be non-zero (dirty) to reach commit.
    cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$*" in
    *"diff --cached --quiet"*) exit 1 ;;
esac
STUB
    chmod +x "$TMP/bin/git"
    run bash "$SCRIPT" commit-dirty "$DIR" 20260101T000000Z
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"git add -A"* ]]
    [[ "$(cat "$CALLS")" == *"IncubaCloud rebuild 20260101T000000Z"* ]]
}

@test "resolve-conflicts keeps the new side of a copier merge" {
    cat > "$DIR/prod.yaml" <<'YAML'
services:
<<<<<<< before
  odoo:
    image: old
=======
  odoo:
    image: new
>>>>>>> after
YAML
    run bash "$SCRIPT" resolve-conflicts "$DIR"
    [ "$status" -eq 0 ]
    grep -q "image: new" "$DIR/prod.yaml"
    ! grep -q "image: old" "$DIR/prod.yaml"
    ! grep -q "<<<<<<<" "$DIR/prod.yaml"
}

@test "resolve-conflicts is a no-op on clean files" {
    printf 'services:\n  odoo:\n    image: odoo\n' > "$DIR/prod.yaml"
    cp "$DIR/prod.yaml" "$TMP/before"
    run bash "$SCRIPT" resolve-conflicts "$DIR"
    [ "$status" -eq 0 ]
    diff "$TMP/before" "$DIR/prod.yaml"
}

@test "boot-test clones the DB, boots against a throwaway PG and cleans up" {
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    # One copy, by pg_basebackup in a container of its own on the
    # database's network namespace, straight into a volume (#91).
    [[ "$calls" == *"--name ic_boot_copy_42 --network container:db123 -v ic_boot_42:/out --user postgres --entrypoint pg_basebackup ghcr.io/tecnativa/postgres-autoconf:17-alpine -h 127.0.0.1 -U odoo -D /out -Fp -X stream --checkpoint=fast --no-sync"* ]]
    [[ "$calls" == *"-v ic_boot_42:/var/lib/postgresql/data"* ]]
    [[ "$calls" != *"compose cp"* ]]
    [[ "$calls" != *"/tmp/ic_boot_42"* ]]
    [[ "$calls" != *"chown -R"* ]]
    # The database name reaches the boot script as its $0, after the body.
    [[ "$calls" == *'click-odoo-update --database "$0"'* ]]
    [[ "$calls" == *"exit 1 prod"* ]]
    # cleanup removed the throwaway PG
    [[ "$calls" == *"rm -f ic_boot_pg_42"* ]]
}

@test "boot-test keeps the throwaway PG off the project network" {
    # The regression that took every instance on a host down, twice: a
    # container holding an endpoint on the project network stops docker
    # compose from recreating that network, so the step dies with 'has
    # active endpoints' and leaves the stack half stopped.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"--network myproj_default"* ]]
    # On a network of its own instead, which the boot container joins.
    # Nothing is published on a host address: on a staging the project
    # network is ``internal`` and a port on its gateway is unreachable.
    [[ "$calls" == *"network create ic_boot_42"* ]]
    [[ "$calls" == *"--network ic_boot_42"* ]]
    [[ "$calls" == *"network connect ic_boot_42 ic_boot_odoo_42"* ]]
    [[ "$calls" == *"PGHOST=ic_boot_pg_42"* ]]
    [[ "$calls" != *" -p "* ]]
}

@test "boot-test bounds the wait for the throwaway postgres" {
    # Doodba's own wait loops forever: a database the boot container
    # cannot reach hung the first staging rebuild in production for good.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"WAIT_DB=false"* ]]
    [[ "$calls" == *'seq 120'* ]]
    [[ "$calls" == *"the throwaway postgres never answered"* ]]
}

@test "boot-test aborts before booting when the network cannot be created" {
    # Failing here is safe: the caller marks the step stop_on_failure, so
    # the instance keeps running its previous image.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
. "$BOOT_ANSWERS"
case "$*" in
    "network create "*)        exit 1 ;;
    *"compose ps -q --status running"*) printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*) shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"click-odoo-update"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

@test "boot-test removes the boot container and its network on the way out" {
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    # The last of each: the pre-cleanup removes them too, before creating.
    last_rm_odoo="$(printf '%s\n' "$calls" | grep -n 'rm -f ic_boot_odoo_42$' | tail -1 | cut -d: -f1)"
    last_net_rm="$(printf '%s\n' "$calls" | grep -n 'network rm ic_boot_42' | tail -1 | cut -d: -f1)"
    wait_line="$(printf '%s\n' "$calls" | grep -n '^docker wait ic_boot_odoo_42' | head -1 | cut -d: -f1)"
    [ -n "$last_rm_odoo" ] && [ -n "$last_net_rm" ] && [ -n "$wait_line" ]
    [ "$last_rm_odoo" -gt "$wait_line" ]
    [ "$last_net_rm" -gt "$last_rm_odoo" ]
}

@test "boot-test puts back the services it found running" {
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"compose ps -q --status running"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
    # 'start', never 'up': up would re-evaluate the image and deploy the
    # very build the boot test just rejected, and would reconcile
    # networks, so the restore could trip over the same drift as the
    # failure it is cleaning up after.
    [[ "$calls" != *"compose up"* ]]
}

@test "boot-test restores the stack even when a step blows up mid-way" {
    # ``set -e`` skips any cleanup written at the tail of the block, and
    # that is precisely when the stack most needs putting back. Here the
    # copy fails: nothing after it runs, but the trap still does.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
. "$BOOT_ANSWERS"
case "$*" in
    *"pg_basebackup"*)         exit 1 ;;
    *"compose ps -q --status running"*) printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*) shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"click-odoo-update"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
    [[ "$calls" == *"rm -f ic_boot_pg_42"* ]]
}

@test "boot-test readies the volume for postgres from a container, never on the host" {
    # The SSH user lacks CAP_CHOWN, and PostgreSQL refuses a data directory
    # in the 755 Docker creates: owner postgres and 700, set inside a
    # container over the volume, which also reads its free space.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"docker volume create ic_boot_42"* ]]
    [[ "$calls" == *"run --rm -v ic_boot_42:/out --user root --entrypoint sh ghcr.io/tecnativa/postgres-autoconf:17-alpine -c chown postgres:postgres /out && chmod 700 /out && df -Pk /out"* ]]
}

@test "boot-test keeps the copy's backup_label" {
    # Without it the copy starts as after a crash, from a checkpoint the
    # files copied before it may not reach; with it, from the backup.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" != *"backup_label"* ]]
}

@test "boot-test removes what a killed run left before it copies" {
    # A run killed before its trap leaves its containers and its volume;
    # pg_basebackup refuses a target that is not empty.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    calls="$(cat "$CALLS")"
    basebackup_line="$(printf '%s\n' "$calls" | grep -n 'pg_basebackup' | head -1 | cut -d: -f1)"
    copy_rm_line="$(printf '%s\n' "$calls" | grep -n 'rm -f ic_boot_copy_42' | head -1 | cut -d: -f1)"
    volume_rm_line="$(printf '%s\n' "$calls" | grep -n 'volume rm ic_boot_42' | head -1 | cut -d: -f1)"
    create_line="$(printf '%s\n' "$calls" | grep -n 'volume create ic_boot_42' | head -1 | cut -d: -f1)"
    [ "$copy_rm_line" -lt "$volume_rm_line" ]
    [ "$volume_rm_line" -lt "$create_line" ]
    [ "$create_line" -lt "$basebackup_line" ]
    # The copy releases before 1.0.228 made inside the database's container.
    [[ "$calls" == *"compose exec -T db rm -rf /tmp/ic_boot_backup_42"* ]]
}

@test "boot-test lets the copy in by replication on the loopback, and takes it back" {
    # postgres-autoconf writes its pg_hba.conf on every start with no
    # replication rule for 127.0.0.1 (spike of 6-nov-2026): the test adds
    # one, marked, reloads, and its trap removes that line and reloads.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    # Replication for the database's own user, not for ``all``.
    rule='host replication odoo 127.0.0.1/32 trust # incubacloud boot test'
    add_line="$(printf '%s\n' "$calls" | grep -nF "printf \"%s\\n\" \"\$1\" >> \"\$2\" sh $rule /etc/postgres/pg_hba.conf" | head -1 | cut -d: -f1)"
    drop_line="$(printf '%s\n' "$calls" | grep -nF "grep -vxF \"\$1\" \"\$2\"" | head -1 | cut -d: -f1)"
    basebackup_line="$(printf '%s\n' "$calls" | grep -n 'pg_basebackup' | head -1 | cut -d: -f1)"
    boot_pg_line="$(printf '%s\n' "$calls" | grep -n 'run -d --name ic_boot_pg_42' | head -1 | cut -d: -f1)"
    [ -n "$add_line" ] && [ -n "$drop_line" ] && [ -n "$boot_pg_line" ]
    [ "$add_line" -lt "$basebackup_line" ]
    # Out right after the copy, not when the test ends; and once.
    [ "$drop_line" -gt "$basebackup_line" ]
    [ "$drop_line" -lt "$boot_pg_line" ]
    [ "$(grep -cF 'grep -vxF' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'SELECT pg_reload_conf()' "$CALLS")" -eq 2 ]
    # The drop reloads in the same call, as the database's own user.
    drop_call="$(printf '%s\n' "$calls" | sed -n "${drop_line}p")"
    [[ "$drop_call" == *'psql -U "$3" -d "$4" -tAc "SELECT pg_reload_conf()"'*" sh $rule /etc/postgres/pg_hba.conf odoo prod" ]]
}

@test "boot-test takes the rule out of a real pg_hba.conf and leaves the rest" {
    hba="$TMP/pg_hba.conf"
    printf 'local all all trust\nhost all all 10.0.0.0/8 md5\n' > "$hba"
    cp "$hba" "$TMP/pg_hba.before"
    STUB_HBA_FILE="$hba" run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    cmp "$hba" "$TMP/pg_hba.before"
    [ ! -e "$TMP/ic_boot_hba" ]
    [[ "$output" != *"WARNING"* ]]
    # Reloaded once with the rule and once without it.
    [ "$(grep -c '^psql .*SELECT pg_reload_conf()' "$CALLS")" -eq 2 ]
    cmp "$TMP/hba.loaded" "$TMP/pg_hba.before"
}

@test "boot-test never empties pg_hba.conf when its temporary copy cannot be written" {
    # A full disk, the case the test risks: the copy of the file without
    # the rule cannot be written. The file stays as it is, rule included,
    # and the log says so, from the drop after the copy and from the trap.
    hba="$TMP/pg_hba.conf"
    printf 'local all all trust\n' > "$hba"
    mkdir "$TMP/ic_boot_hba"
    STUB_HBA_FILE="$hba" run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    grep -qx 'local all all trust' "$hba"
    grep -qxF 'host replication odoo 127.0.0.1/32 trust # incubacloud boot test' "$hba"
    [ "$(grep -c "WARNING: could not take the boot test's replication rule out of the database's $hba" <<<"$output")" -eq 2 ]
    # No reload of a file the drop did not change: the one after the rule
    # went in only.
    [ "$(grep -c '^psql .*SELECT pg_reload_conf()' "$CALLS")" -eq 1 ]
}

@test "boot-test stopped during the copy takes the rule out first, then the volume" {
    # A stop sends TERM to the step's whole session and KILL 60 s later:
    # the rule goes before anything that may take long.
    hba="$TMP/pg_hba.conf"
    printf 'local all all trust\n' > "$hba"
    cp "$hba" "$TMP/pg_hba.before"
    printf '#!/usr/bin/env bash\necho "sleep $*" >> "$CALLS"\n/bin/sleep 0.1\n' > "$TMP/bin/sleep"
    chmod +x "$TMP/bin/sleep"
    STUB_HBA_FILE="$hba" STUB_COPY_BLOCKS=1 \
        setsid bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod \
        >"$TMP/out" 2>&1 </dev/null &
    pid=$!
    for _ in $(seq 100); do
        [ -e "$TMP/copying" ] && break
        /bin/sleep 0.1
    done
    [ -e "$TMP/copying" ]
    grep -qF 'incubacloud boot test' "$hba"
    pkill -TERM -s "$pid"
    status=0
    wait "$pid" || status=$?
    [ "$status" -ne 0 ]
    cmp "$hba" "$TMP/pg_hba.before"
    calls="$(cat "$CALLS")"
    drop_line="$(printf '%s\n' "$calls" | grep -nF 'grep -vxF' | head -1 | cut -d: -f1)"
    volume_rm_line="$(printf '%s\n' "$calls" | grep -n 'volume rm ic_boot_42' | tail -1 | cut -d: -f1)"
    copy_rm_line="$(printf '%s\n' "$calls" | grep -n 'rm -f ic_boot_copy_42' | tail -1 | cut -d: -f1)"
    basebackup_line="$(printf '%s\n' "$calls" | grep -n 'pg_basebackup' | head -1 | cut -d: -f1)"
    [ -n "$drop_line" ] && [ -n "$volume_rm_line" ] && [ -n "$copy_rm_line" ]
    [ "$drop_line" -gt "$basebackup_line" ]
    [ "$drop_line" -lt "$copy_rm_line" ]
    [ "$copy_rm_line" -lt "$volume_rm_line" ]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
    [[ "$calls" != *"click-odoo-update"* ]]
}

@test "boot-test stopped as a detached step does, TERM after TERM, still runs its whole trap" {
    # detached_step.sh stop sends TERM to the session, and the step's
    # ``timeout`` sends it again to its process group: a TERM that landed
    # while the trap ran used to kill it there, the rule left live and the
    # volume kept (spike server, 13-nov-2026). The rule's removal is made
    # slow here so the second TERM lands inside the trap.
    hba="$TMP/pg_hba.conf"
    printf 'local all all trust\n' > "$hba"
    cp "$hba" "$TMP/pg_hba.before"
    printf '#!/usr/bin/env bash\necho "sleep $*" >> "$CALLS"\n/bin/sleep 0.1\n' > "$TMP/bin/sleep"
    chmod +x "$TMP/bin/sleep"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    "compose exec -T db sh -c "*grep\ -vxF*) : > "$TMP/dropping"; /bin/sleep 1 ;;
esac
. "$BOOT_ANSWERS"
case "$*" in
    *"compose config --services"*)    printf 'odoo\ndb\nsmtp\n' ;;
    *"compose ps -q --status running"*)    printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*)    shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    STUB_HBA_FILE="$hba" STUB_COPY_BLOCKS=1 \
        setsid timeout --kill-after=60 60 bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod \
        >"$TMP/out" 2>&1 </dev/null &
    pid=$!
    for _ in $(seq 100); do
        [ -e "$TMP/copying" ] && break
        /bin/sleep 0.1
    done
    [ -e "$TMP/copying" ]
    pkill -TERM -s "$pid"
    for _ in $(seq 100); do
        [ -e "$TMP/dropping" ] && break
        /bin/sleep 0.05
    done
    [ -e "$TMP/dropping" ]
    pkill -TERM -s "$pid" || true
    status=0
    wait "$pid" || status=$?
    [ "$status" -eq 143 ]
    cmp "$hba" "$TMP/pg_hba.before"
    calls="$(cat "$CALLS")"
    drop_line="$(printf '%s\n' "$calls" | grep -nF 'grep -vxF' | head -1 | cut -d: -f1)"
    volume_rm_line="$(printf '%s\n' "$calls" | grep -n 'volume rm ic_boot_42' | tail -1 | cut -d: -f1)"
    [ -n "$drop_line" ] && [ "$volume_rm_line" -gt "$drop_line" ]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

@test "boot-test stopped as a detached step keeps its trap's docker calls out of the later TERMs" {
    # The trap ignores the TERMs after the first one, and the docker client
    # does not: it sets its own handlers and dies of them, and the trap's
    # ``|| true`` hid it. Here the stub's slow part dies of a TERM as the
    # client does, and a TERM lands while the rule is taken out (after the
    # file was edited, before the reload) and another while the services
    # are started again. The stub keeps what docker would hold: the
    # containers and the volume, the rule as loaded, the services started.
    hba="$TMP/pg_hba.conf"
    printf 'local all all trust\n' > "$hba"
    cp "$hba" "$TMP/pg_hba.before"
    printf '#!/usr/bin/env bash\necho "sleep $*" >> "$CALLS"\n/bin/sleep 0.1\n' > "$TMP/bin/sleep"
    chmod +x "$TMP/bin/sleep"
    mkdir "$TMP/state"
    cat > "$TMP/bin/psql" <<'STUB'
#!/usr/bin/env bash
echo "psql $*" >> "$CALLS"
if ! grep -qF 'incubacloud boot test' "$STUB_HBA_FILE"; then
    : > "$TMP/dropping"
    env --default-signal=TERM /bin/sleep 1 || exit 143
fi
cp "$STUB_HBA_FILE" "$TMP/hba.loaded"
STUB
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    "run "*)
        prev=""
        for arg; do
            [ "$prev" != --name ] || : > "$TMP/state/container.$arg"
            prev="$arg"
        done ;;
    "rm -f "*)
        shift 2
        for name; do rm -f "$TMP/state/container.$name"; done ;;
    "volume create "*)    : > "$TMP/state/volume.$3" ;;
    "volume rm "*)        rm -f "$TMP/state/volume.$3" ;;
    "compose start "*)
        : > "$TMP/starting"
        env --default-signal=TERM /bin/sleep 1 || exit 143
        shift 2
        echo "$*" > "$TMP/state/started" ;;
esac
. "$BOOT_ANSWERS"
case "$*" in
    *"compose config --services"*)    printf 'odoo\ndb\nsmtp\n' ;;
    *"compose ps -q --status running"*)    printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*)    shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    STUB_HBA_FILE="$hba" STUB_COPY_BLOCKS=1 \
        setsid timeout --kill-after=60 60 bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod \
        >"$TMP/out" 2>&1 </dev/null &
    pid=$!
    for marker in copying dropping starting; do
        for _ in $(seq 100); do
            [ -e "$TMP/$marker" ] && break
            /bin/sleep 0.05
        done
        [ -e "$TMP/$marker" ]
        pkill -TERM -s "$pid" || true
    done
    status=0
    wait "$pid" || status=$?
    [ "$status" -eq 143 ]
    # The rule out of the file and out of the database's memory.
    cmp "$hba" "$TMP/pg_hba.before"
    cmp "$TMP/hba.loaded" "$TMP/pg_hba.before"
    [[ "$(cat "$TMP/out")" != *"WARNING"* ]]
    # The copy's container and the volume gone, the instance started.
    [ -z "$(find "$TMP/state" -name 'container.*' -o -name 'volume.*')" ]
    [ "$(cat "$TMP/state/started")" = "db odoo smtp" ]
}

@test "boot-test says what it measured and how long the copy should take" {
    STUB_DB_KB=$((50 * 1024 * 1024)) run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    [[ "$output" == *"the database takes 50.0 GiB, Docker's disk has 100.0 GiB free and the test needs 60.0 GiB"* ]]
    # 50 GiB at 150 MB/s is about 358 s; the budget an hour and 537 s.
    [[ "$output" == *"the copy should take about 6 min, and the test has 69 min"* ]]
    [[ "$output" == *"copied in "* ]]
}

@test "boot-test stops before copying when the copy does not fit, and puts everything back" {
    STUB_DB_KB=$((512 * 1024 * 1024)) STUB_FREE_KB=$((300 * 1024 * 1024)) \
        run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"boot test stopped: No room on the server for the boot test: the database takes 512.0 GiB, Docker's disk has 300.0 GiB free and the test needs 568.2 GiB"* ]]
    [[ "$output" == *"Nothing was changed."* ]]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"pg_basebackup"* ]]
    [[ "$calls" != *"pg_hba.conf"* ]]
    [[ "$calls" != *"click-odoo-update"* ]]
    [[ "$calls" == *"volume rm ic_boot_42"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

@test "boot-test stops before copying when its budget would pass the step's cap" {
    # 2 TiB at 100 MB/s and an hour is about 7 h: more than a 6 h step.
    STUB_DB_KB=$((2048 * 1024 * 1024)) STUB_FREE_KB=$((4096 * 1024 * 1024)) \
        run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod 21600
    [ "$status" -eq 1 ]
    [[ "$output" == *"boot test stopped: The boot test would need about 7 h for a database of 2048.0 GiB"* ]]
    [[ "$output" == *"more than the 6 h a step may run"* ]]
    [[ "$(cat "$CALLS")" != *"pg_basebackup"* ]]
}

@test "boot-test cuts a copy that runs past its budget, and says so" {
    cat > "$TMP/bin/timeout" <<'STUB'
#!/usr/bin/env bash
echo "timeout $*" >> "$CALLS"
case "$*" in *pg_basebackup*) exit 124 ;; esac
while [ "${1#-}" != "$1" ]; do shift; done
shift
exec "$@"
STUB
    chmod +x "$TMP/bin/timeout"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"boot test stopped: The boot test ran past its time budget of 61 min while it copied the database"* ]]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"timeout --kill-after=60 3610 docker run --name ic_boot_copy_42"* ]]
    [[ "$calls" != *"click-odoo-update"* ]]
    [[ "$calls" == *"rm -f ic_boot_copy_42"* ]]
    [[ "$calls" == *"volume rm ic_boot_42"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

# A copy killed by a KILL (137) that was not timeout's, which comes only
# after the budget and its minute more.
copy_killed_stub() {
    cat > "$TMP/bin/timeout" <<'STUB'
#!/usr/bin/env bash
echo "timeout $*" >> "$CALLS"
case "$*" in *pg_basebackup*) exit 137 ;; esac
while [ "${1#-}" != "$1" ]; do shift; done
shift
exec "$@"
STUB
    chmod +x "$TMP/bin/timeout"
}

@test "boot-test says a copy the kernel killed ran out of memory" {
    copy_killed_stub
    STUB_COPY_OOM=true run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"boot test stopped: The copy of the database ran out of memory: the server's kernel killed it after 0 min. Nothing was changed."* ]]
    [[ "$output" != *"time budget"* ]]
    calls="$(cat "$CALLS")"
    # Read before the container goes, and the rule out before both.
    inspect_line="$(printf '%s\n' "$calls" | grep -n 'State.OOMKilled' | head -1 | cut -d: -f1)"
    copy_rm_line="$(printf '%s\n' "$calls" | grep -n 'rm -f ic_boot_copy_42' | tail -1 | cut -d: -f1)"
    drop_line="$(printf '%s\n' "$calls" | grep -nF 'grep -vxF' | head -1 | cut -d: -f1)"
    [ -n "$inspect_line" ] && [ -n "$drop_line" ]
    [ "$drop_line" -lt "$inspect_line" ]
    [ "$inspect_line" -lt "$copy_rm_line" ]
    [[ "$calls" != *"click-odoo-update"* ]]
}

@test "boot-test gives both causes of a KILL it cannot tell apart, before the budget" {
    copy_killed_stub
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"boot test stopped: The copy of the database was killed after 0 min, before its time budget of 61 min ran out: the server ran out of memory, or something removed its container. Nothing was changed."* ]]
}

@test "boot-test stops when the database is not running, before it copies" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    "compose ps -q db") exit 0 ;;
esac
. "$BOOT_ANSWERS"
case "$*" in
    *"compose config --services"*)    printf 'odoo\ndb\nsmtp\n' ;;
    *"compose ps -q --status running"*)    printf 'odoo\nsmtp\n' ;;
    "inspect -f "*)    shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"boot test stopped: The database of the instance is not running"* ]]
    [[ "$(cat "$CALLS")" != *"pg_basebackup"* ]]
}

@test "boot-test propagates a failed boot as a non-zero exit" {
    # Make the click-odoo-update run fail; cleanup must still happen and
    # the script must exit non-zero (so stop_on_failure keeps the old image).
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
. "$BOOT_ANSWERS"
case "$*" in
    *"click-odoo-update"*) exit 1 ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 1 ]
    # Cleanup still ran despite the failure.
    [[ "$(cat "$CALLS")" == *"rm -f ic_boot_pg_42"* ]]
}

@test "boot-test reports the boot container's own exit code" {
    # The container runs detached, so its outcome comes from docker wait:
    # a boot that fails inside it must still fail the step.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
. "$BOOT_ANSWERS"
case "$*" in
    "wait "*)                  echo 3 ;;
    *"compose ps -q --status running"*) printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*) shift 3; printf '%s\n' "$@" ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 3 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"compose start db odoo smtp"* ]]
    [[ "$calls" == *"network rm ic_boot_42"* ]]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" resolve-conflicts "$HOME/projects/gone"
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}

@test "copier-update without a ref moves to latest and says so" {
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"copier update"* ]]
    [[ "$(cat "$CALLS")" != *"--vcs-ref"* ]]
    [[ "$output" == *"unpinned"* ]]
}

@test "copier-update pins the template when a ref is given" {
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" "v3.2.1"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--vcs-ref v3.2.1"* ]]
}

@test "commit-dirty excludes the live log before it stages anything" {
    # Excluding after ``git add -A`` would still leave copier facing a
    # tree Odoo had dirtied in between, so the order is the fix and not
    # merely the exclusion. Production: the rebuild died on "Destination
    # repository is dirty" after 26 later steps had already succeeded.
    mkdir -p "$DIR/.git/info"
    run bash "$SCRIPT" commit-dirty "$DIR" 20260824T003439Z
    [ "$status" -eq 0 ]
    grep -qxF '/logs/' "$DIR/.git/info/exclude"
    untrack_at="$(grep -n 'ls-files --error-unmatch logs' "$CALLS" | head -1 | cut -d: -f1)"
    stage_at="$(grep -n 'add -A' "$CALLS" | head -1 | cut -d: -f1)"
    [ -n "$untrack_at" ]
    [ "$untrack_at" -lt "$stage_at" ]
}

@test "commit-dirty excludes continuous recovery's directory before it stages anything" {
    mkdir -p "$DIR/.git/info"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 0 ]
    grep -qxF '/.walg/' "$DIR/.git/info/exclude"
    untrack_at="$(grep -n 'ls-files --error-unmatch .walg' "$CALLS" | head -1 | cut -d: -f1)"
    stage_at="$(grep -n 'add -A' "$CALLS" | head -1 | cut -d: -f1)"
    [ -n "$untrack_at" ]
    [ "$untrack_at" -lt "$stage_at" ]
}

# A real repository, with git itself rather than the stub, and a copier
# that refuses a dirty tree as copier's ``run_update`` does, with
# ``git status --porcelain``. The instance has continuous recovery on,
# whose agent writes its spool inside the project.
_instance_repo_with_pitr() {
    rm -f "$TMP/bin/git"
    cat > "$HOME/.local/bin/copier" <<'STUB'
#!/usr/bin/env bash
echo "copier $*" >> "$CALLS"
if [ -n "$(git -C "${!#}" status --porcelain)" ]; then
    echo "Destination repository is dirty; cannot continue." >&2
    exit 1
fi
STUB
    git -C "$DIR" init -q
    printf 'template\n' > "$DIR/prod.yaml"
    mkdir -p "$DIR/.walg/spool/done"
    printf 'wal-g\n' > "$DIR/.walg/wal-g"
    : > "$DIR/.walg/spool/done/00000001000000000000000B"
    printf '1761566560\n' > "$DIR/.walg/spool/fs-ok"
}

@test "copier-update runs on the commit while the pitr agent writes its spool" {
    # QA 7016, 2026-10-27: the first commit took the spool in, the agent
    # wrote it again before copier looked, and the plan change failed
    # on "Destination repository is dirty", holding the instance's queue.
    _instance_repo_with_pitr
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 0 ]
    [ "$(git -C "$DIR" ls-files prod.yaml)" = "prod.yaml" ]
    # The agent, between the commit and copier's check: a segment
    # uploaded, an hour-old mark dropped, a filestore copy recorded.
    : > "$DIR/.walg/spool/done/000000010000000000000018"
    rm "$DIR/.walg/spool/done/00000001000000000000000B"
    printf '1761566860\n' > "$DIR/.walg/spool/fs-ok"
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"copier update"* ]]
    [[ "$output" != *"dirty"* ]]
}

@test "copier-update names a change made after the commit instead of running copier" {
    # copier only says the tree is dirty, never what: the path is what
    # tells someone else's change from the product's own.
    _instance_repo_with_pitr
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 0 ]
    printf 'edited on the host\n' >> "$DIR/prod.yaml"
    printf 'note\n' > "$DIR/notes.txt"
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR"*"prod.yaml"* ]]
    [[ "$output" == *"notes.txt"* ]]
    [[ "$(cat "$CALLS")" != *"copier update"* ]]
}

# A copier project as a deploy leaves it, with git itself rather than the
# stub: the template's SSH directory (with a key someone put there), the
# repository tokens the panel writes into repos.yaml, and an Odoo conf
# holding a password.
_instance_repo_with_secrets() {
    rm -f "$TMP/bin/git"
    git -C "$DIR" init -q
    mkdir -p "$DIR/odoo/custom/ssh" "$DIR/odoo/custom/src" "$DIR/odoo/custom/conf.d" \
        "$DIR/odoo/custom/dependencies"
    printf 'template\n' > "$DIR/prod.yaml"
    printf '_commit: v10.0.8\n' > "$DIR/.copier-answers.yml"
    printf -- '-----BEGIN OPENSSH PRIVATE KEY-----\nIC_FAKE_KEY\n' > "$DIR/odoo/custom/ssh/id_rsa"
    printf 'ssh-ed25519 IC_FAKE_PUB\n' > "$DIR/odoo/custom/ssh/id_rsa.pub"
    printf 'github.com ssh-ed25519 AAAA\n' > "$DIR/odoo/custom/ssh/known_hosts"
    printf 'Host *\n' > "$DIR/odoo/custom/ssh/config"
    printf 'priv:\n  remotes:\n    priv: https://x-access-token:IC_FAKE_TOKEN@github.com/o/priv.git\n' \
        > "$DIR/odoo/custom/src/repos.yaml"
    printf 'odoo: {}\n' > "$DIR/odoo/custom/src/addons.yaml"
    printf 'git+https://IC_FAKE_PIP_TOKEN@github.com/o/lib.git\n' \
        > "$DIR/odoo/custom/dependencies/pip.txt"
    printf 'curl\n' > "$DIR/odoo/custom/dependencies/apt.txt"
    printf '[options]\nadmin_passwd = IC_FAKE_PASSWORD\n' > "$DIR/odoo/custom/conf.d/inst.conf"
    : > "$DIR/odoo/custom/conf.d/.empty"
}

# Whether a secret of the fixture is in the repository: index or history.
_repo_holds_secrets() {
    git -C "$DIR" ls-files | grep -E '^odoo/custom/ssh/|repos\.yaml$|conf\.d/.*\.conf$|pip\.txt$' \
        || git -C "$DIR" log --all -p | grep -E 'IC_FAKE_(KEY|PUB|TOKEN|PASSWORD|PIP_TOKEN)'
}

@test "commit-dirty keeps the project's secrets out of the repository" {
    # The template's ssh/ (doodba copies it into the image as root's
    # ~/.ssh, so a deploy key goes there), the tokens in repos.yaml and a
    # password in the Odoo conf went into every rebuild's commit.
    _instance_repo_with_secrets
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 0 ]
    run _repo_holds_secrets
    [ "$status" -ne 0 ]
    # What copier owns is committed as before, and every file stays on disk.
    [ "$(git -C "$DIR" ls-files prod.yaml)" = "prod.yaml" ]
    [ "$(git -C "$DIR" ls-files odoo/custom/conf.d)" = "odoo/custom/conf.d/.empty" ]
    [ "$(git -C "$DIR" ls-files odoo/custom/dependencies)" = "odoo/custom/dependencies/apt.txt" ]
    grep -q IC_FAKE_PIP_TOKEN "$DIR/odoo/custom/dependencies/pip.txt"
    grep -q IC_FAKE_KEY "$DIR/odoo/custom/ssh/id_rsa"
    grep -q IC_FAKE_TOKEN "$DIR/odoo/custom/src/repos.yaml"
    grep -q IC_FAKE_PASSWORD "$DIR/odoo/custom/conf.d/inst.conf"
    [ -z "$(git -C "$DIR" status --porcelain)" ]
}

@test "commit-dirty stops before committing a secret it could not keep out" {
    # Fail closed: an exclude file it cannot write lets ``git add -A``
    # stage the secret again. Aborting the rebuild can be retried; a
    # commit stays in the history.
    [ "$(id -u)" -ne 0 ] || skip "root writes a read-only exclude file anyway"
    _instance_repo_with_secrets
    mkdir -p "$DIR/.git/info"
    : > "$DIR/.git/info/exclude"
    chmod 444 "$DIR/.git/info/exclude"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR"*"odoo/custom/src/repos.yaml"* ]]
    [[ "$output" == *"odoo/custom/ssh/id_rsa"* ]]
    [ -z "$(git -C "$DIR" rev-parse -q --verify HEAD)" ]
    run _repo_holds_secrets
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" log --all -p 2>/dev/null)" ]
}

@test "commit-dirty keeps out a secret staged by a commit that failed" {
    # The index holds what neither HEAD nor the disk has: a commit that
    # failed after staging, then a deploy that rewrote repos.yaml.
    _instance_repo_with_secrets
    git -C "$DIR" add -A
    git -C "$DIR" -c user.email=t@example.com -c user.name=T commit -qm "an earlier rebuild"
    printf 'priv: {remotes: {priv: "https://x-access-token:IC_FAKE_TOKEN2@github.com/o/priv.git"}}\n' \
        > "$DIR/odoo/custom/src/repos.yaml"
    git -C "$DIR" add -A
    printf 'priv: {remotes: {priv: "https://x-access-token:IC_FAKE_TOKEN3@github.com/o/priv.git"}}\n' \
        > "$DIR/odoo/custom/src/repos.yaml"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" ls-tree -r --name-only HEAD | grep -E 'repos\.yaml$')" ]
    [ -z "$(git -C "$DIR" log --all -p | grep 'IC_FAKE_TOKEN[23]')" ]
    grep -q IC_FAKE_TOKEN3 "$DIR/odoo/custom/src/repos.yaml"
}

@test "commit-dirty takes out of the index the secrets an earlier rebuild committed" {
    _instance_repo_with_secrets
    git -C "$DIR" add -A
    git -C "$DIR" -c user.email=t@example.com -c user.name=T commit -qm "an earlier rebuild"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261027T120743Z
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" ls-tree -r --name-only HEAD | grep -E '^odoo/custom/ssh/|repos\.yaml$|conf\.d/.*\.conf$|pip\.txt$')" ]
    grep -q IC_FAKE_PIP_TOKEN "$DIR/odoo/custom/dependencies/pip.txt"
    grep -q IC_FAKE_KEY "$DIR/odoo/custom/ssh/id_rsa"
    grep -q IC_FAKE_TOKEN "$DIR/odoo/custom/src/repos.yaml"
    [ -z "$(git -C "$DIR" status --porcelain)" ]
}


# ── a staging rendered by template v10: odoo lives in the gatekeeper's namespace ──

_gatekeeper_docker() {
    # docker as a v10 staging answers it: the gatekeeper is a service, and
    # its container is up. The first ``STUB_CONFIG_FAILS`` reads of the
    # services fail.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
. "$BOOT_ANSWERS"
case "$*" in
    "wait "*)                          echo 0 ;;
    *"compose config --services"*)
        if [ "$(grep -c 'compose config --services' "$CALLS")" -le "${STUB_CONFIG_FAILS:-0}" ]; then
            echo "stub: cannot read the compose" >&2
            exit 1
        fi
        printf 'db\ngatekeeper\nsmtp\nodoo\n' ;;
    *"compose ps -q gatekeeper"*)      echo gk123 ;;
    *"compose ps -q --status running"*)         printf 'odoo\ndb\nsmtp\ngatekeeper\n' ;;
    "inspect -f "*)         shift 3; printf '%s\n' "$@" ;;
esac
STUB
}

@test "boot-test under the gatekeeper puts the throwaway PG in its namespace" {
    # Docker refuses to connect a container that shares a network
    # namespace to any other network: the first pull request preview
    # under template v10 died on that (QA, 2026-10-21). The throwaway PG
    # joins the gatekeeper's namespace and the boot container, which
    # shares it, reaches it on the loopback; nothing is created or
    # connected on a network of its own.
    _gatekeeper_docker
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"--network container:gk123"* ]]
    [[ "$calls" == *"PGHOST=127.0.0.1"* ]]
    [[ "$calls" != *"network create ic_boot_42"* ]]
    [[ "$calls" != *"network connect"* ]]
    [[ "$calls" == *'click-odoo-update --database "$0"'* ]]
    # The clean-up is the same: the containers go, the services come back.
    [[ "$calls" == *"rm -f ic_boot_pg_42"* ]]
    [[ "$calls" == *"compose start db gatekeeper odoo smtp"* ]]
}

@test "boot-test retries a failed compose read and still finds the gatekeeper" {
    _gatekeeper_docker
    export STUB_CONFIG_FAILS=1
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    [[ "$output" == *"retrying once"* ]]
    calls="$(cat "$CALLS")"
    [ "$(grep -c 'compose config --services' "$CALLS")" -eq 2 ]
    # Read before the database is copied, so a failed read costs no copy.
    [ "$(grep -n -m 1 'compose config --services' "$CALLS" | cut -d: -f1)" \
        -lt "$(grep -n -m 1 'pg_basebackup' "$CALLS" | cut -d: -f1)" ]
    [[ "$calls" == *"--network container:gk123"* ]]
    [[ "$calls" != *"network create ic_boot_42"* ]]
}

@test "boot-test stops when it cannot read the compose, never booting as if there were no gatekeeper" {
    # QA, walk 6f6: the read failed, the test took the staging for one
    # without a gatekeeper, put the throwaway PG on a network the boot
    # container could not join, and failed with nothing to say why.
    _gatekeeper_docker
    export STUB_CONFIG_FAILS=99
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    [[ "$output" == *"stub: cannot read the compose"* ]]
    [[ "$output" == *"could not read the compose to know whether the boot test needs the gatekeeper"* ]]
    calls="$(cat "$CALLS")"
    # It stops before copying the database, so there is nothing to put
    # back, and it never takes the gatekeeper for one it started.
    [[ "$calls" != *"pg_basebackup"* ]]
    [[ "$calls" != *"network create ic_boot_42"* ]]
    [[ "$calls" != *"postgres-autoconf"* ]]
    [[ "$calls" != *"click-odoo-update"* ]]
    [[ "$calls" != *"compose up"* ]]
    [[ "$calls" != *"rm -sf gatekeeper"* ]]
}

_first_gatekeeper_docker() {
    # docker as a staging rendered before v10 answers it on its first
    # rebuild under v10: the files declare a gatekeeper, none runs until
    # ``compose up`` starts it. $1 is the boot container's exit code;
    # $2, when ``fail``, makes the gatekeeper refuse to start.
    cat > "$TMP/bin/docker" <<STUB
#!/usr/bin/env bash
echo "docker \$*" >> "\$CALLS"
. "\$BOOT_ANSWERS"
case "\$*" in
    "wait "*)                          echo ${1:-0} ;;
    *"compose config --services"*)     printf 'db\ngatekeeper\nsmtp\nodoo\n' ;;
    *"compose up "*"gatekeeper"*)      [ "${2:-}" = fail ] && exit 1
                                       touch "$TMP/gk-up" ;;
    *"compose ps -q gatekeeper"*)      [ -e "$TMP/gk-up" ] && echo gk456 ;;
    *"compose ps -q --status running"*)         printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*)         shift 3; printf '%s\n' "\$@" ;;
esac
exit 0
STUB
    chmod +x "$TMP/bin/docker"
}

@test "boot-test starts the gatekeeper a staging rendered before v10 does not have yet" {
    # acme-staging, the first staging rebuilt under v10
    # (2026-10-22): copier had rewritten its files, but the old odoo
    # still ran without a gatekeeper and the test refused to boot.
    _first_gatekeeper_docker 0
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    # That service alone: nothing else is recreated under the live stack.
    [[ "$calls" == *"compose up -d --no-deps --wait --wait-timeout 120 gatekeeper"* ]]
    [[ "$calls" == *"--network container:gk456"* ]]
    [[ "$calls" == *"compose run --label traefik.enable=false --label sablier.enable=false -d --no-deps --name ic_boot_odoo_42"* ]]
    # It passed: the gatekeeper stays for the restart that follows.
    [[ "$calls" != *"compose rm -sf gatekeeper"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

@test "boot-test removes the gatekeeper it started when the boot fails" {
    _first_gatekeeper_docker 1
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    calls="$(cat "$CALLS")"
    # The stack is left as it was found: the old model, no gatekeeper.
    [[ "$calls" == *"compose rm -sf gatekeeper"* ]]
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

@test "boot-test stops when the gatekeeper it needs will not start" {
    _first_gatekeeper_docker 0 fail
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    [[ "$output" == *"the gatekeeper did not start"* ]]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"--name ic_boot_pg_42"* ]]
    [[ "$calls" == *"compose rm -sf gatekeeper"* ]]
    # Whatever was running is put back all the same.
    [[ "$calls" == *"compose start db odoo smtp"* ]]
}

@test "boot-test never lets compose touch the services the boot depends on" {
    # Without --no-deps Compose recreates any dependency whose definition
    # the template changed: under v10, the live db, mid-test.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"compose run --label traefik.enable=false --label sablier.enable=false -d --no-deps --name ic_boot_odoo_42"* ]]
}

@test "boot-test without a gatekeeper keeps its own network, as before" {
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"network create ic_boot_42"* ]]
    [[ "$calls" == *"run -d --name ic_boot_pg_42 --network ic_boot_42"* ]]
    # Only the copy shares a namespace, the database's.
    [ "$(grep -c 'container:' "$CALLS")" -eq 1 ]
    [[ "$calls" == *"--network container:db123"* ]]
}

# An 8.0 instance after a fetch-repos cut short: trees on disk, a
# temporary one, the build's Dockerfile.dockerignore, and no exclusions.
_instance_repo_with_trees() {
    _instance_repo_with_secrets
    mkdir -p "$DIR/odoo/custom/src/odoo/addons" "$DIR/odoo/custom/src/addons/mod" \
        "$DIR/odoo/custom/src/.ic-tmp-web" "$DIR/odoo/custom/src/private/mine"
    echo IC_FAKE_TREE > "$DIR/odoo/custom/src/odoo/setup.py"
    echo IC_FAKE_TREE > "$DIR/odoo/custom/src/addons/mod/__openerp__.py"
    echo IC_FAKE_TREE > "$DIR/odoo/custom/src/.ic-tmp-web/x"
    echo '!custom/src/odoo' > "$DIR/odoo/Dockerfile.dockerignore"
    echo "{'name': 'mine'}" > "$DIR/odoo/custom/src/private/mine/__openerp__.py"
}

@test "commit-dirty keeps fetched repository trees out of the history, private stays in" {
    _instance_repo_with_trees
    run bash "$SCRIPT" commit-dirty "$DIR" 20261108T120000Z
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" ls-files -- odoo/custom/src | grep -vE '^odoo/custom/src/(private/|addons\.yaml$)')" ]
    [ -z "$(git -C "$DIR" ls-files odoo/Dockerfile.dockerignore)" ]
    [ "$(git -C "$DIR" ls-files odoo/custom/src/private)" = "odoo/custom/src/private/mine/__openerp__.py" ]
    [ -z "$(git -C "$DIR" log --all -p | grep IC_FAKE_TREE)" ]
    [ -f "$DIR/odoo/custom/src/odoo/setup.py" ]
    [ -z "$(git -C "$DIR" status --porcelain)" ]
}

@test "commit-dirty takes out of the index trees an earlier commit took" {
    _instance_repo_with_trees
    git -C "$DIR" add -A
    git -C "$DIR" -c user.email=t@example.com -c user.name=T commit -qm "an earlier rebuild"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261108T120000Z
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" ls-tree -r --name-only HEAD | grep -E '^odoo/custom/src/(odoo|addons|\.ic-tmp-web)/|Dockerfile\.dockerignore')" ]
    [ -n "$(git -C "$DIR" ls-tree -r --name-only HEAD | grep '^odoo/custom/src/private/')" ]
}

@test "commit-dirty stops before committing a tree it could not keep out" {
    [ "$(id -u)" -ne 0 ] || skip "root writes a read-only exclude file anyway"
    _instance_repo_with_trees
    rm -rf "$DIR/odoo/custom/ssh" "$DIR/odoo/custom/conf.d"/*.conf \
        "$DIR/odoo/custom/src/repos.yaml" "$DIR/odoo/custom/dependencies/pip.txt"
    mkdir -p "$DIR/.git/info"
    : > "$DIR/.git/info/exclude"
    chmod 444 "$DIR/.git/info/exclude"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261108T120000Z
    [ "$status" -eq 1 ]
    [[ "$output" == *"fetched repository trees are still in the repository's index"* ]]
    [ -z "$(git -C "$DIR" rev-parse -q --verify HEAD)" ]
}


# ── the mark of a rebuild left unapplied (#96) ──────────────────────────

# A real repository, committed clean, and a copier that writes whether the
# mark was there when it ran, and what it held, to $TMP/mark-at-copier.
_instance_repo_for_the_mark() {
    rm -f "$TMP/bin/git"
    cat > "$HOME/.local/bin/copier" <<'STUB'
#!/usr/bin/env bash
echo "copier $*" >> "$CALLS"
dir="${!#}"
if [ -f "$dir/.incubacloud-unapplied" ]; then
    cp "$dir/.incubacloud-unapplied" "$TMP/mark-at-copier"
else
    echo none > "$TMP/mark-at-copier"
fi
STUB
    chmod +x "$HOME/.local/bin/copier"
    git -C "$DIR" init -q
    printf 'template\n' > "$DIR/prod.yaml"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261113T120000Z
    [ "$status" -eq 0 ]
}

# Write a mark as copier-update writes it.
#   _mark <job> <kind>
_mark() {
    printf 'job=%s\nkind=%s\nat=2026-11-13T12:00:00Z\n' "$1" "$2" \
        > "$DIR/.incubacloud-unapplied"
}

@test "copier-update lays the mark of its job before copier runs" {
    _instance_repo_for_the_mark
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    [ "$status" -eq 0 ]
    grep -qx 'job=512' "$TMP/mark-at-copier"
    grep -qx 'kind=build' "$TMP/mark-at-copier"
    grep -q '^at=20[0-9-]*T[0-9:]*Z$' "$TMP/mark-at-copier"
    # Copier's run leaves it, for the restart to lift.
    grep -qx 'job=512' "$DIR/.incubacloud-unapplied"
}

@test "a mark cut between its write and its rename leaves git nothing to see" {
    _instance_repo_for_the_mark
    local real_mv
    real_mv="$(command -v mv)"
    cat > "$TMP/bin/mv" <<STUB
#!/usr/bin/env bash
case "\$*" in *incubacloud-unapplied*) exit 1 ;; esac
exec "$real_mv" "\$@"
STUB
    chmod +x "$TMP/bin/mv"
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    rm -f "$TMP/bin/mv"
    [ "$status" -ne 0 ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
    ls "$DIR"/.git/.incubacloud-unapplied.tmp.*
    [ -z "$(git -C "$DIR" status --porcelain --untracked-files=all)" ]
}

@test "copier-update lays no mark on a tree it refuses as dirty" {
    _instance_repo_for_the_mark
    printf 'edited on the host\n' >> "$DIR/prod.yaml"
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    [ "$status" -eq 1 ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
    [ ! -e "$TMP/mark-at-copier" ]
}

@test "copier-update stops before copier when git would see the mark" {
    [ "$(id -u)" -ne 0 ] || skip "root writes a read-only exclude file anyway"
    _instance_repo_for_the_mark
    : > "$DIR/.git/info/exclude"
    chmod 444 "$DIR/.git/info/exclude"
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR"*".incubacloud-unapplied"* ]]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
    [ ! -e "$TMP/mark-at-copier" ]
}

@test "copier-update lays no mark in a project without a repository" {
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    [ "$status" -eq 0 ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
}

@test "copier-update of a plan change stops at a rebuild's mark, without copier" {
    _instance_repo_for_the_mark
    _mark 400 build
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 files
    [ "$status" -eq 26 ]
    [[ "$output" == *"rebuild #400"* ]]
    [ ! -e "$TMP/mark-at-copier" ]
    grep -qx 'job=400' "$DIR/.incubacloud-unapplied"
    grep -qx 'kind=build' "$DIR/.incubacloud-unapplied"
}

@test "copier-update of a rebuild takes the place of another job's mark" {
    _instance_repo_for_the_mark
    _mark 400 build
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    [ "$status" -eq 0 ]
    grep -qx 'job=512' "$TMP/mark-at-copier"
    grep -qx 'kind=build' "$TMP/mark-at-copier"
}

@test "copier-update of a plan change takes the place of another plan change's mark" {
    _instance_repo_for_the_mark
    _mark 400 files
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 files
    [ "$status" -eq 0 ]
    grep -qx 'job=512' "$TMP/mark-at-copier"
    grep -qx 'kind=files' "$TMP/mark-at-copier"
}

@test "copier-update writes its own job's mark again on a second attempt" {
    _instance_repo_for_the_mark
    _mark 512 files
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 files
    [ "$status" -eq 0 ]
    grep -qx 'job=512' "$TMP/mark-at-copier"
}

@test "copier-update without a job neither lays nor reads a mark (the warm claim)" {
    _instance_repo_for_the_mark
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/mark-at-copier")" = none ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
    # Another job's mark is left as it is, and copier runs.
    rm -f "$TMP/mark-at-copier"
    _mark 400 build
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8
    [ "$status" -eq 0 ]
    grep -qx 'job=400' "$TMP/mark-at-copier"
}

@test "copier-update refuses a kind it does not know" {
    _instance_repo_for_the_mark
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 image
    [ "$status" -eq 1 ]
    [ ! -e "$TMP/mark-at-copier" ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
}

@test "the mark never shows in git status, and commit-dirty keeps it out of the index" {
    _instance_repo_for_the_mark
    run bash "$SCRIPT" copier-update "$DIR" "$TMP/answers.yml" v10.0.8 512 build
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" status --porcelain --untracked-files=all)" ]
    # One committed before the exclusion existed is taken out of the index.
    git -C "$DIR" add -f .incubacloud-unapplied
    git -C "$DIR" -c user.email=t@example.com -c user.name=T commit -qm "a mark taken in"
    run bash "$SCRIPT" commit-dirty "$DIR" 20261113T130000Z
    [ "$status" -eq 0 ]
    [ -z "$(git -C "$DIR" ls-files .incubacloud-unapplied)" ]
    [ -f "$DIR/.incubacloud-unapplied" ]
}

@test "clear-unapplied of a job lifts only that job's mark" {
    _mark 400 build
    run bash "$SCRIPT" clear-unapplied "$DIR" 512
    [ "$status" -eq 0 ]
    grep -qx 'job=400' "$DIR/.incubacloud-unapplied"
    run bash "$SCRIPT" clear-unapplied "$DIR" 400
    [ "$status" -eq 0 ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
    # No mark at all is nothing to do.
    run bash "$SCRIPT" clear-unapplied "$DIR" 400
    [ "$status" -eq 0 ]
}

@test "clear-unapplied without a job lifts any mark, the way out by hand" {
    _mark 400 build
    run bash "$SCRIPT" clear-unapplied "$DIR"
    [ "$status" -eq 0 ]
    [ ! -e "$DIR/.incubacloud-unapplied" ]
    [[ "$output" == *"rebuild #400"* ]]
}

@test "check-unapplied says 0 without a mark and 26 with one, naming job and kind" {
    run bash "$SCRIPT" check-unapplied "$DIR"
    [ "$status" -eq 0 ]
    _mark 400 build
    run bash "$SCRIPT" check-unapplied "$DIR"
    [ "$status" -eq 26 ]
    [[ "$output" == *"IC_UNAPPLIED job=400 kind=build"* ]]
    _mark 401 files
    run bash "$SCRIPT" check-unapplied "$DIR"
    [ "$status" -eq 26 ]
    [[ "$output" == *"IC_UNAPPLIED job=401 kind=files"* ]]
}

@test "check-unapplied takes a mark it cannot read for a rebuild's" {
    printf 'garbage\n' > "$DIR/.incubacloud-unapplied"
    run bash "$SCRIPT" check-unapplied "$DIR"
    [ "$status" -eq 26 ]
    [[ "$output" == *"kind=build"* ]]
}

@test "check-unapplied finds no mark where there is no directory" {
    run bash "$SCRIPT" check-unapplied "$HOME/projects/gone"
    [ "$status" -eq 0 ]
}
