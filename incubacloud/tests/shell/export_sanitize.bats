#!/usr/bin/env bats
# End-to-end tests for scripts/export_sanitize.sh.
#
# Nothing is stubbed here: a fake instance folder full of secrets is
# built on disk, the script runs for real (rsync, sed, tar), and the
# resulting tarball is extracted and inspected. That is a far stronger
# check than the string-matching the executor's f-string used to allow —
# these tests fail if the sanitizer *believes* it removed a secret but
# the file still ends up in the archive.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/export_sanitize.sh"
    command -v rsync >/dev/null || skip "rsync is not installed"

    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    INST="$HOME/projects/inst"
    STAGING="$TMP/staging"
    ARCHIVE="$TMP/export.tar.gz"
    OUT="$TMP/extracted"

    mkdir -p "$INST/.docker" "$INST/odoo/custom/src" "$INST/odoo/custom/ssh" \
             "$INST/odoo/auto" "$INST/dumps" "$INST/postgres" \
             "$INST/secrets" "$INST/.git"

    # Files that must survive.
    echo "keep me" > "$INST/README.md"
    cat > "$INST/.copier-answers.yml" <<'YAML'
odoo_version: "19.0"
backup_dst: s3://secret-bucket/path
project_name: inst
YAML
    cat > "$INST/odoo/custom/src/repos.yaml" <<'YAML'
./odoo:
  remotes:
    origin: https://x-access-token:ghp_supersecrettoken@github.com/acme/odoo.git
YAML

    # Secrets that must not.
    echo "ADMIN_PASSWORD=hunter2" > "$INST/.docker/odoo.env"
    echo "PGPASSWORD=hunter2" > "$INST/.docker/db-access.env"
    echo "POSTGRES_PASSWORD=hunter2" > "$INST/.docker/db-creation.env"
    echo "AWS_SECRET_ACCESS_KEY=hunter2" > "$INST/.docker/backup.env"
    echo "INCUBACLOUD_SECRET_KEY=fernetkey" > "$INST/.docker/incubacloud.env"
    echo "PRIVATE KEY" > "$INST/odoo/custom/ssh/id_rsa"
    echo "PUBLIC KEY" > "$INST/odoo/custom/ssh/id_rsa.pub"
    echo "host ssh-ed25519 AAAA" > "$INST/odoo/custom/ssh/known_hosts"
    echo "override" > "$INST/docker-compose.override.yml"
    echo "build artefact" > "$INST/odoo/auto/addons.txt"
    echo "dump" > "$INST/dumps/prod.sql"
    echo "pgdata" > "$INST/postgres/pg.conf"
    echo "secret" > "$INST/secrets/token"
    echo "gitdata" > "$INST/.git/config"

    mkdir -p "$OUT"
}

teardown() {
    rm -rf "$TMP"
}

run_export() {
    run bash "$SCRIPT" "$INST" "$STAGING" "$ARCHIVE"
    [ "$status" -eq 0 ]
    tar -xzf "$ARCHIVE" -C "$OUT"
}

@test "the archive is built and its size reported" {
    run_export
    [ -f "$ARCHIVE" ]
    [[ "$output" == *"SIZE:"* ]]
}

@test "ordinary project files survive" {
    run_export
    [ "$(cat "$OUT/README.md")" = "keep me" ]
}

@test "the Fernet key file is removed, not placeholdered" {
    # The deploy only creates incubacloud.env when absent, so a
    # placeholder would survive as an unusable key.
    run_export
    [ ! -f "$OUT/.docker/incubacloud.env" ]
}

@test "the backup credentials file is removed" {
    run_export
    [ ! -f "$OUT/.docker/backup.env" ]
}

@test "env files a redeploy needs keep their shape with placeholders" {
    run_export
    [ "$(cat "$OUT/.docker/odoo.env")" = "ADMIN_PASSWORD=changeme" ]
    [ "$(cat "$OUT/.docker/db-access.env")" = "PGPASSWORD=changeme" ]
    [ "$(cat "$OUT/.docker/db-creation.env")" = "POSTGRES_PASSWORD=changeme" ]
}

@test "no real password reaches the archive" {
    run_export
    ! grep -rq "hunter2" "$OUT"
}

@test "SSH material is removed" {
    run_export
    [ ! -f "$OUT/odoo/custom/ssh/id_rsa" ]
    [ ! -f "$OUT/odoo/custom/ssh/id_rsa.pub" ]
    [ ! -f "$OUT/odoo/custom/ssh/known_hosts" ]
}

@test "repository tokens are stripped but the URL still works" {
    run_export
    ! grep -q "x-access-token" "$OUT/odoo/custom/src/repos.yaml"
    grep -q "https://github.com/acme/odoo.git" "$OUT/odoo/custom/src/repos.yaml"
}

@test "the backup destination is dropped from the copier answers" {
    run_export
    ! grep -q "backup_dst" "$OUT/.copier-answers.yml"
    grep -q "odoo_version" "$OUT/.copier-answers.yml"
}

@test "data directories and VCS checkouts are excluded" {
    run_export
    for excluded in .git odoo/auto dumps postgres secrets \
                    docker-compose.override.yml; do
        [ ! -e "$OUT/$excluded" ] || {
            echo "leaked: $excluded"
            return 1
        }
    done
}

@test "the staging copy is cleaned up" {
    run_export
    [ ! -d "$STAGING" ]
}

@test "a stale staging directory does not leak into the archive" {
    mkdir -p "$STAGING"
    echo "leftover" > "$STAGING/stale.txt"
    run_export
    [ ! -f "$OUT/stale.txt" ]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" "$HOME/projects/gone" "$STAGING" "$ARCHIVE"
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    run bash "$SCRIPT" "~/projects/inst" "$STAGING" "$ARCHIVE"
    [ "$status" -eq 0 ]
}
