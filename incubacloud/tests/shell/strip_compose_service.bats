#!/usr/bin/env bats
# Tests for scripts/strip_compose_service.sh — remove a service block
# from docker-compose files. Runs on real files (no stubs).

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/strip_compose_service.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"
}

teardown() {
    rm -rf "$TMP"
}

_write_prod() {
    cat > "$DIR/prod.yaml" <<'YAML'
services:
  odoo:
    image: odoo
  smtp:
    image: ""
    env_file:
      - .docker/smtp.env
  db:
    image: postgres
volumes:
  data:
YAML
}

@test "removes the named service block and nothing else" {
    _write_prod
    run bash "$SCRIPT" "$DIR" smtp prod.yaml
    [ "$status" -eq 0 ]
    ! grep -q "smtp:" "$DIR/prod.yaml"
    grep -q "odoo:" "$DIR/prod.yaml"
    grep -q "db:" "$DIR/prod.yaml"
    grep -q "volumes:" "$DIR/prod.yaml"
}

@test "leaves the file untouched when the service is absent" {
    _write_prod
    cp "$DIR/prod.yaml" "$TMP/before"
    run bash "$SCRIPT" "$DIR" backup prod.yaml
    [ "$status" -eq 0 ]
    diff "$TMP/before" "$DIR/prod.yaml"
}

@test "processes several files, skipping missing ones" {
    _write_prod
    cat > "$DIR/common.yaml" <<'YAML'
services:
  backup:
    image: ""
  odoo:
    image: odoo
YAML
    run bash "$SCRIPT" "$DIR" backup prod.yaml common.yaml missing.yaml
    [ "$status" -eq 0 ]
    ! grep -q "backup:" "$DIR/common.yaml"
    grep -q "odoo:" "$DIR/common.yaml"
}

@test "a tilde instance directory resolves against HOME" {
    _write_prod
    run bash "$SCRIPT" "~/projects/inst" smtp prod.yaml
    [ "$status" -eq 0 ]
    ! grep -q "smtp:" "$DIR/prod.yaml"
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" "$HOME/projects/gone" smtp prod.yaml
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "too few arguments are refused" {
    run bash "$SCRIPT" "$DIR" smtp
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}
