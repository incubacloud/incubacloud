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
    # which services were running, so it can put them back, and the exit
    # code of the boot container, which it reads with ``docker wait``. A
    # stub that answers nothing makes the step fail, which is correct
    # behaviour and not what these tests exercise.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    "wait "*)                     echo 0 ;;
    *"compose config --services"*)    printf 'odoo\ndb\nsmtp\n' ;;
    *"compose ps -q --status running"*)    printf 'odoo\ndb\nsmtp\n' ;;
    "inspect -f "*)    shift 3; printf '%s\n' "$@" ;;
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
    # The database name reaches the boot script as its $0, after the body.
    [[ "$calls" == *'click-odoo-update --database "$0"'* ]]
    [[ "$calls" == *"exit 1 prod"* ]]
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
    # clone copy fails: nothing after it runs, but the trap still does.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"compose cp"*)            exit 1 ;;
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

@test "boot-test reports the boot container's own exit code" {
    # The container runs detached, so its outcome comes from docker wait:
    # a boot that fails inside it must still fail the step.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
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
    # firma1-staging, the canary of the v10 bump in production
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
    [[ "$calls" != *"postgres-autoconf"* ]]
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
    [[ "$calls" == *"--network ic_boot_42"* ]]
    [[ "$calls" != *"container:"* ]]
}
