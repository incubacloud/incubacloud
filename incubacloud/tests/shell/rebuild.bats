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
    # The boot test asks docker two questions it cannot proceed without:
    # the gateway of the project network (it publishes the throwaway PG
    # there rather than joining the network) and the host port docker
    # picked for it. It also asks which services were running, so it can
    # put them back. A stub that answers nothing makes the step abort,
    # which is correct behaviour and not what these tests exercise.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"network inspect"*)          echo "172.18.0.1" ;;
    "port "*)                     echo "172.18.0.1:49153" ;;
    *"compose ps --services"*)    printf 'odoo\ndb\nsmtp\n' ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
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
    # when sibling jobs (warm pool) target the same host. Already set here,
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
    [[ "$calls" == *"pg_basebackup -U odoo -D /tmp/ic_boot_backup_42"* ]]
    [[ "$calls" == *"postgres-autoconf:17-alpine"* ]]
    [[ "$calls" == *"click-odoo-update --database prod"* ]]
    # cleanup removed the throwaway PG
    [[ "$calls" == *"rm -f ic_boot_pg_42"* ]]
}

@test "boot-test keeps the throwaway PG off the project network" {
    # The regression that took every tenant on a host down, twice: a
    # container holding an endpoint on the project network stops docker
    # compose from recreating that network, so the step dies with 'has
    # active endpoints' and leaves the stack half stopped.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"--network myproj_default"* ]]
    # Reached through the network's gateway instead: visible to every
    # container on that bridge, invisible to compose.
    [[ "$calls" == *"network inspect myproj_default"* ]]
    [[ "$calls" == *"-p 172.18.0.1::5432"* ]]
    [[ "$calls" == *"PGHOST=172.18.0.1"* ]]
    [[ "$calls" == *"PGPORT=49153"* ]]
}

@test "boot-test aborts when the gateway cannot be resolved" {
    # Falling back to the old on-network behaviour would reintroduce the
    # outage silently. Failing here is safe: the caller marks the step
    # stop_on_failure, so the instance keeps running its previous image.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"network inspect"*) exit 1 ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    [[ "$output" == *"cannot resolve the gateway"* ]]
    [[ "$(cat "$CALLS")" != *"click-odoo-update"* ]]
}

@test "boot-test puts back the services it found running" {
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"compose ps --services --status running"* ]]
    [[ "$calls" == *"compose start odoo db smtp"* ]]
    # 'start', never 'up': up would re-evaluate the image and deploy the
    # very build the boot test just rejected, and would reconcile
    # networks, so the restore could trip over the same drift as the
    # failure it is cleaning up after.
    [[ "$calls" != *"compose up"* ]]
}

@test "boot-test restores the stack even when a step blows up mid-way" {
    # ``set -e`` skips any cleanup written at the tail of the block, and
    # that is precisely when the stack most needs putting back. Here the
    # clone copy fails: nothing after it runs, but the trap still does.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"compose cp"*)            exit 1 ;;
    *"network inspect"*)       echo "172.18.0.1" ;;
    "port "*)                  echo "172.18.0.1:49153" ;;
    *"compose ps --services"*) printf 'odoo\ndb\nsmtp\n' ;;
esac
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [ "$status" -ne 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" != *"click-odoo-update"* ]]
    [[ "$calls" == *"compose start odoo db smtp"* ]]
    [[ "$calls" == *"rm -f ic_boot_pg_42"* ]]
}

@test "boot-test chowns the clone inside a container, never on the host" {
    # The SSH user lacks CAP_CHOWN, so the chown to UID 70 must run inside
    # an ephemeral root container over the bind mount.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    [[ "$(cat "$CALLS")" == *"docker run --rm -v /tmp/ic_boot_42:/data alpine chown -R 70:70 /data"* ]]
}

@test "boot-test removes backup_label inside the db container before copying out" {
    # Once the mount is chowned to UID 70 the host can't touch it, so
    # backup_label must be dropped in-container, before docker compose cp.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    calls="$(cat "$CALLS")"
    label_line="$(printf '%s\n' "$calls" | grep -n 'rm -f /tmp/ic_boot_backup_42/backup_label' | head -1 | cut -d: -f1)"
    cp_line="$(printf '%s\n' "$calls" | grep -n 'compose cp db:/tmp/ic_boot_backup_42' | head -1 | cut -d: -f1)"
    [ -n "$label_line" ]
    [ "$label_line" -lt "$cp_line" ]
}

@test "boot-test scrubs both sides before pg_basebackup" {
    # pg_basebackup refuses a non-empty target, and the host bind mount may
    # carry a UID-70 dir from an interrupted run — both must be wiped first.
    run bash "$SCRIPT" boot-test "$DIR" 42 myproj odoo 17 prod
    calls="$(cat "$CALLS")"
    basebackup_line="$(printf '%s\n' "$calls" | grep -n 'pg_basebackup' | head -1 | cut -d: -f1)"
    host_scrub_line="$(printf '%s\n' "$calls" | grep -n 'rm -rf /host_tmp/ic_boot_42' | head -1 | cut -d: -f1)"
    [ "$host_scrub_line" -lt "$basebackup_line" ]
}

@test "boot-test propagates a failed boot as a non-zero exit" {
    # Make the click-odoo-update run fail; cleanup must still happen and
    # the script must exit non-zero (so stop_on_failure keeps the old image).
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
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

