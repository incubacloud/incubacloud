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
             "$INST/odoo/custom/conf.d" "$INST/odoo/custom/dependencies" \
             "$INST/odoo/auto" "$INST/dumps" "$INST/postgres" \
             "$INST/secrets" "$INST/.git" "$INST/logs"

    # Files that must survive.
    echo "keep me" > "$INST/README.md"
    echo "services: {}" > "$INST/devel.yaml"
    echo "services: {}" > "$INST/prod.yaml"
    ln -s prod.yaml "$INST/docker-compose.yml"
    echo "requests==2.32.3" > "$INST/odoo/custom/dependencies/pip.txt"
    echo "web: ['*']" > "$INST/odoo/custom/src/addons.yaml"
    cat > "$INST/.copier-answers.yml" <<'YAML'
odoo_version: "19.0"
backup_dst: s3://secret-bucket/path
backup_email_to: ops@example.com
smtp_relay_host: smtp.example.com
smtp_relay_user: relay-user
project_name: inst
smtp_default_from: odoo@example.com
YAML
    cat > "$INST/odoo/custom/conf.d/inst.conf" <<'CONF'
[options]
admin_passwd = hunter2
db_password=hunter2
smtp_password: hunter2
api_token = hunter2
some_secret = hunter2
workers = 2
proxy_mode = True
x_real_ip_header = CF-Connecting-IP
x_real_ip_trusted = 173.245.48.0/20
CONF
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
    echo "INCUBACLOUD_SECRET_KEY=hunter2" > "$INST/.docker/incubacloud.env"
    echo "RELAY_PASSWORD=hunter2" > "$INST/.docker/smtp.env"
    echo "RELAY_PASSWORD=hunter2" > "$INST/.docker/smtp.env.bak"
    echo "COMPOSE_PROJECT_NAME=inst-production" > "$INST/.env"
    echo "TOKEN=hunter2" > "$INST/.env.bak"
    echo "TOKEN=hunter2" > "$INST/odoo/custom/src/extra.env"
    echo "odoo log line" > "$INST/logs/odoo.log"
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

@test "what a developer needs survives" {
    run_export
    [ -f "$OUT/devel.yaml" ]
    [ -f "$OUT/prod.yaml" ]
    [ -f "$OUT/odoo/custom/dependencies/pip.txt" ]
    [ -f "$OUT/odoo/custom/src/addons.yaml" ]
    [ -f "$OUT/odoo/custom/src/repos.yaml" ]
}

@test "no env file of any kind reaches the archive" {
    # devel.yaml reads none, so none is exported — not even as a
    # placeholder — wherever it lives and whatever it is called.
    run_export
    [ ! -e "$OUT/.docker" ]
    [ -z "$(find "$OUT" \( -name '.env' -o -name '.env.*' \
                           -o -name '*.env' -o -name '*.env.*' \))" ]
}

@test "the instance's logs are left out" {
    run_export
    [ ! -e "$OUT/logs" ]
}

@test "the Odoo conf loses its credentials and production proxies" {
    run_export
    conf="$OUT/odoo/custom/conf.d/inst.conf"
    # ``! cmd`` never fails a bats test unless it is the last line.
    run grep -Eiq "passw|secret|token" "$conf"
    [ "$status" -eq 1 ]
    run grep -q "x_real_ip" "$conf"
    [ "$status" -eq 1 ]
    grep -q "^\[options\]" "$conf"
    grep -q "^workers = 2" "$conf"
    grep -q "^proxy_mode = True" "$conf"
}

@test "docker-compose.yml points at the development stack" {
    run_export
    [ -L "$OUT/docker-compose.yml" ]
    [ "$(readlink "$OUT/docker-compose.yml")" = "devel.yaml" ]
}

@test "the link is left alone when there is no devel.yaml" {
    rm "$INST/devel.yaml"
    run_export
    [ "$(readlink "$OUT/docker-compose.yml")" = "prod.yaml" ]
}

@test "a DEVELOPMENT.md explains how to start the copy" {
    run_export
    grep -q "invoke git-aggregate" "$OUT/DEVELOPMENT.md"
    grep -q "Download Neutralized Backup" "$OUT/DEVELOPMENT.md"
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
    run grep -q "x-access-token" "$OUT/odoo/custom/src/repos.yaml"
    [ "$status" -eq 1 ]
    grep -q "https://github.com/acme/odoo.git" "$OUT/odoo/custom/src/repos.yaml"
}

@test "backup and SMTP relay settings are dropped from the copier answers" {
    run_export
    run grep -Eq "^(backup_|smtp_relay_)" "$OUT/.copier-answers.yml"
    [ "$status" -eq 1 ]
    grep -q "odoo_version" "$OUT/.copier-answers.yml"
    grep -q "smtp_default_from" "$OUT/.copier-answers.yml"
}

@test "data directories and VCS checkouts are excluded" {
    run_export
    for excluded in .git odoo/auto dumps postgres secrets logs \
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
