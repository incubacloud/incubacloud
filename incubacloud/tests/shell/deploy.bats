#!/usr/bin/env bats
# Tests for scripts/deploy.sh. File-mutating operations run on real
# files; the container/copier operations stub docker/copier/python3.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/deploy.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR/.docker"

    mkdir -p "$TMP/bin" "$HOME/.local/bin"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    for tool in docker python3; do
        cat > "$TMP/bin/$tool" <<STUB
#!/usr/bin/env bash
echo "$tool \$*" >> "\$CALLS"
STUB
        chmod +x "$TMP/bin/$tool"
    done
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

@test "teardown-previous tears down and removes an existing dir" {
    run bash "$SCRIPT" teardown-previous "$DIR"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose down --volumes --rmi all --remove-orphans"* ]]
    [ ! -d "$DIR" ]
}

@test "teardown-previous is a no-op on a clean slate" {
    run bash "$SCRIPT" teardown-previous "$HOME/projects/none"
    [ "$status" -eq 0 ]
    [[ "$output" == *"clean slate"* ]]
    [ ! -s "$CALLS" ]
}

@test "ensure-secret-key generates a key only when absent" {
    run bash "$SCRIPT" ensure-secret-key "$DIR"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"python3 -c"* ]]
}

@test "ensure-secret-key keeps an existing key untouched" {
    echo "INCUBACLOUD_SECRET_KEY=existing" > "$DIR/.docker/incubacloud.env"
    run bash "$SCRIPT" ensure-secret-key "$DIR"
    [ "$status" -eq 0 ]
    [ ! -s "$CALLS" ]
    [ "$(cat "$DIR/.docker/incubacloud.env")" = "INCUBACLOUD_SECRET_KEY=existing" ]
}

@test "inject-secret-env targets prod.yaml and test.yaml, not common.yaml" {
    for f in prod.yaml test.yaml common.yaml; do
        printf 'services:\n  odoo:\n    env_file:\n      - .docker/odoo.env\n' \
            > "$DIR/$f"
    done
    run bash "$SCRIPT" inject-secret-env "$DIR"
    [ "$status" -eq 0 ]
    grep -q "incubacloud.env" "$DIR/prod.yaml"
    grep -q "incubacloud.env" "$DIR/test.yaml"
    ! grep -q "incubacloud.env" "$DIR/common.yaml"
}

@test "inject-secret-env is idempotent" {
    printf 'services:\n  odoo:\n    env_file:\n      - .docker/odoo.env\n' \
        > "$DIR/prod.yaml"
    bash "$SCRIPT" inject-secret-env "$DIR"
    bash "$SCRIPT" inject-secret-env "$DIR"
    [ "$(grep -c 'incubacloud.env' "$DIR/prod.yaml")" -eq 1 ]
}

@test "cap-backup-hostname shortens an over-long hostname" {
    long="backup.$(printf 'a%.0s' {1..70}).example.com"
    printf 'services:\n  backup:\n    hostname: %s\n' "$long" > "$DIR/prod.yaml"
    cp "$DIR/prod.yaml" "$DIR/common.yaml"
    run bash "$SCRIPT" cap-backup-hostname "$DIR" myproj
    [ "$status" -eq 0 ]
    [[ "$output" == *"capped backup hostname"* ]]
    ! grep -q "$long" "$DIR/prod.yaml"
    grep -q "hostname: backup.myproj" "$DIR/prod.yaml"
}

@test "cap-backup-hostname leaves a short hostname alone" {
    printf 'services:\n  backup:\n    hostname: backup.short.io\n' \
        > "$DIR/prod.yaml"
    run bash "$SCRIPT" cap-backup-hostname "$DIR" myproj
    [ "$status" -eq 0 ]
    grep -q "hostname: backup.short.io" "$DIR/prod.yaml"
}

@test "copier-deploy exports the pipx PATH and runs copier" {
    run bash "$SCRIPT" copier-deploy "$DIR" /tmp/answers.yml gh:Tecnativa/tmpl
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"copier copy --defaults --overwrite --trust --data-file /tmp/answers.yml gh:Tecnativa/tmpl $DIR"* ]]
}

@test "copier-deploy reads the git default branch before writing it" {
    # Read-first guard: an unconditional write takes the ~/.gitconfig lock
    # and races sibling deploys on the same host (warm-pool cron). Here git
    # reports the value is already set, so no write must follow.
    cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$*" in
    *"--get init.defaultBranch"*) echo "master"; exit 0 ;;
esac
STUB
    chmod +x "$TMP/bin/git"
    run bash "$SCRIPT" copier-deploy "$DIR" /tmp/answers.yml gh:Tecnativa/tmpl
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"git config --global --get init.defaultBranch"* ]]
    # Already set → the script must NOT write it again.
    [[ "$(cat "$CALLS")" != *"git config --global init.defaultBranch master"* ]]
}

@test "copier-deploy seeds the git default branch when unset" {
    cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$*" in
    *"--get init.defaultBranch"*) exit 1 ;;
esac
STUB
    chmod +x "$TMP/bin/git"
    run bash "$SCRIPT" copier-deploy "$DIR" /tmp/answers.yml gh:Tecnativa/tmpl
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"git config --global init.defaultBranch master"* ]]
}

@test "set-system-params writes web.base.url and report.url" {
    run bash "$SCRIPT" set-system-params "$DIR" odoo prod "https://x.io" "http://localhost:8069"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose exec -T db psql -U odoo -d prod"* ]]
    [[ "$(cat "$CALLS")" == *"web.base.url"* ]]
    [[ "$(cat "$CALLS")" == *"https://x.io"* ]]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}

@test "copier-deploy without a ref runs unpinned and says so" {
    run bash "$SCRIPT" copier-deploy "$DIR" "$TMP/answers.yml" "gh:x/y"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"copier copy"* ]]
    [[ "$(cat "$CALLS")" != *"--vcs-ref"* ]]
    # The effective revision must be visible in the job log either way,
    # or an unpinned build leaves no trace of what produced the tree.
    [[ "$output" == *"unpinned"* ]]
}

@test "copier-deploy pins the template when a ref is given" {
    run bash "$SCRIPT" copier-deploy "$DIR" "$TMP/answers.yml" "gh:x/y" "v3.2.1"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--vcs-ref v3.2.1"* ]]
    [[ "$output" == *"v3.2.1"* ]]
}
