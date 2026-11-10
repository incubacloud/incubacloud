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

@test "cap-hostnames cuts the odoo hostname of a long domain at a label" {
    # A name of the shape a deploy met on 10-oct-2026: 70 bytes.
    long="acme-addons-private-production.vps.host-20261010-2457-paid.example.com"
    printf 'services:\n  odoo:\n    hostname: "%s"\n' "$long" > "$DIR/prod.yaml"
    run bash "$SCRIPT" cap-hostnames "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *"capped hostname in prod.yaml"* ]]
    grep -q '    hostname: "acme-addons-private-production.vps.host-20261010-2457-paid"$' \
        "$DIR/prod.yaml"
}

@test "cap-hostnames keeps the service label of every over-long value" {
    long="backup.$(printf 'a%.0s' {1..50}).example.com"
    printf 'services:\n  backup:\n    hostname: %s\n' "$long" > "$DIR/common.yaml"
    printf 'services:\n  odoo:\n    hostname: %s.vps.example.com\n' "$(printf 'a%.0s' {1..50})" \
        > "$DIR/test.yaml"
    run bash "$SCRIPT" cap-hostnames "$DIR"
    [ "$status" -eq 0 ]
    grep -q "^    hostname: backup.$(printf 'a%.0s' {1..50})$" "$DIR/common.yaml"
    grep -q "^    hostname: $(printf 'a%.0s' {1..50}).vps.example$" "$DIR/test.yaml"
}

@test "cap-hostnames cuts a single label longer than 63 bytes" {
    printf 'services:\n  odoo:\n    hostname: %s.io\n' "$(printf 'b%.0s' {1..70})" \
        > "$DIR/prod.yaml"
    run bash "$SCRIPT" cap-hostnames "$DIR"
    [ "$status" -eq 0 ]
    grep -q "^    hostname: $(printf 'b%.0s' {1..63})$" "$DIR/prod.yaml"
}

@test "cap-hostnames leaves short hostnames and other lines alone" {
    printf 'services:\n  odoo:\n    hostname: "www.short.io"\n    image: x\n' \
        > "$DIR/prod.yaml"
    cp "$DIR/prod.yaml" "$TMP/before"
    run bash "$SCRIPT" cap-hostnames "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" != *"capped"* ]]
    cmp "$TMP/before" "$DIR/prod.yaml"
}

@test "copier-deploy exports the pipx PATH and runs copier" {
    run bash "$SCRIPT" copier-deploy "$DIR" /tmp/answers.yml gh:Tecnativa/tmpl
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"copier copy --defaults --overwrite --trust --data-file /tmp/answers.yml gh:Tecnativa/tmpl $DIR"* ]]
}

@test "copier-deploy reads the git default branch before writing it" {
    # Read-first guard: an unconditional write takes the ~/.gitconfig lock
    # and races sibling deploys on the same host (a layer's cron). Here git
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

# A docker whose ``pg_isready`` in ``db`` says "rejecting connections"
# (exit 1, a database still starting up) $1 times before it answers, or
# every time when $1 is ``never``. Every other call succeeds.
_docker_db_starting() {
    export PG_FAILS="$TMP/pg_fails"
    printf '%s\n' "$1" > "$PG_FAILS"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"exec -T db pg_isready"*)
        left="$(cat "$PG_FAILS")"
        [ "$left" = never ] && exit 1
        if [ "$left" -gt 0 ]; then
            echo $((left - 1)) > "$PG_FAILS"
            exit 1
        fi
        ;;
esac
exit 0
STUB
    chmod +x "$TMP/bin/docker"
}

@test "set-system-params waits for a db still starting up before psql" {
    _docker_db_starting 2
    run bash "$SCRIPT" set-system-params "$DIR" odoo prod "https://x.io" "http://localhost:8069"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'exec -T db pg_isready' "$CALLS")" -eq 3 ]
    last_ready="$(grep -n 'exec -T db pg_isready' "$CALLS" | tail -1 | cut -d: -f1)"
    psql_line="$(grep -n 'exec -T db psql' "$CALLS" | head -1 | cut -d: -f1)"
    [ -n "$psql_line" ]
    [ "$last_ready" -lt "$psql_line" ]
}

@test "set-system-params fails without psql when db never accepts connections" {
    _docker_db_starting never
    IC_DB_WAIT_SECONDS=2 run bash "$SCRIPT" set-system-params "$DIR" odoo prod "https://x.io" "http://localhost:8069"
    [ "$status" -ne 0 ]
    [[ "$output" == *"db did not accept connections within 2 s"* ]]
    [ "$(grep -c 'exec -T db psql' "$CALLS")" -eq 0 ]
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

# A git whose ``ls-remote`` answers from $LS_REMOTE_OUT, or fails the way
# git does at a remote it cannot read (a message naming its arguments on
# stderr) when $LS_REMOTE_FAIL is set. Before answering it does what git
# does once a remote took the credentials of its URL: ``git credential
# approve``, with the real git, the options given before ``ls-remote``
# and the environment of the call, so a credential helper the call can
# see stores them. It also records the ``http.extraHeader`` the call
# sees in $CALLS.headers. Everything else is the real git.
_stub_git() {
    local real
    real="$(command -v git)"
    cat > "$TMP/bin/git" <<STUB
#!/usr/bin/env bash
pre=()
while [ "\$#" -gt 0 ] && [ "\$1" != ls-remote ]; do pre+=("\$1"); shift; done
if [ "\${1:-}" = ls-remote ]; then
    echo "git \${pre[*]} \$*" >> "\$CALLS"
    "$real" "\${pre[@]}" config --get-urlmatch http.extraHeader "\$3" >> "\$CALLS.headers"
    if [ -n "\${LS_REMOTE_FAIL:-}" ]; then
        echo "fatal: unable to access '\$*': Could not resolve host" >&2
        exit 128
    fi
    url="\$3"
    if [[ "\$url" =~ ^https://([^:@/]+):([^@/]+)@([^/]+)/ ]]; then
        printf 'protocol=https\nhost=%s\nusername=%s\npassword=%s\n\n' \
            "\${BASH_REMATCH[3]}" "\${BASH_REMATCH[1]}" "\${BASH_REMATCH[2]}" \
            | "$real" "\${pre[@]}" credential approve
    fi
    printf '%s' "\${LS_REMOTE_OUT:-}"
    exit 0
fi
exec "$real" "\${pre[@]}"
STUB
    chmod +x "$TMP/bin/git"
}

# A timeout that runs its command, or behaves as a command that ran out
# of time (status 124, or $LS_REMOTE_HANG_STATUS, nothing run) when
# $LS_REMOTE_HANG is set.
_stub_timeout() {
    cat > "$TMP/bin/timeout" <<'STUB'
#!/usr/bin/env bash
echo "timeout $*" >> "$CALLS"
[ -z "${LS_REMOTE_HANG:-}" ] || exit "${LS_REMOTE_HANG_STATUS:-124}"
[ -z "${LS_REMOTE_HANG_ON:-}" ] || [[ "$*" != *"$LS_REMOTE_HANG_ON"* ]] || exit 124
while [ "$#" -gt 0 ] && [ "$1" != git ]; do shift; done
exec "$@"
STUB
    chmod +x "$TMP/bin/timeout"
}

HEADS_TOKEN="ghs_tokenOfTheHour"
HEADS_URL="https://x-access-token:${HEADS_TOKEN}@github.com/acme/addons.git"
HEADS_SHA="1111111111111111111111111111111111111111"
PIN_SHA="2222222222222222222222222222222222222222"
HEADS_PLAIN_URL="https://github.com/acme/addons.git"

@test "record-heads names the head of each branch in the build context" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "addons main $HEADS_SHA" ]
    [[ "$(cat "$CALLS")" == *"ls-remote -- https://github.com/acme/addons.git refs/heads/main refs/tags/main"* ]]
    [[ "$output" == *"head of addons (main): $HEADS_SHA"* ]]
}

@test "record-heads changes the file when a branch moves, and not otherwise" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    first="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "$first" ]
    export LS_REMOTE_OUT="3333333333333333333333333333333333333333	refs/heads/main
"
    bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" != "$first" ]
}

@test "record-heads takes a pinned commit as it is and asks nothing" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'pinned\tpin\t%s\t\n' "$PIN_SHA" > "$TMP/spec"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "pinned $PIN_SHA $PIN_SHA" ]
    [ ! -s "$CALLS" ]
}

@test "record-heads never prints the token, even when git does" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_FAIL=1
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [[ "$output" != *"$HEADS_TOKEN"* ]]
    ! grep -q "$HEADS_TOKEN" "$DIR/odoo/custom/incubacloud-heads.txt"
    unset LS_REMOTE_FAIL
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [[ "$output" != *"$HEADS_TOKEN"* ]]
    ! grep -q "$HEADS_TOKEN" "$DIR/odoo/custom/incubacloud-heads.txt"
}

@test "record-heads that cannot read a head breaks the cache and warns" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_FAIL=1
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [[ "$output" == *"WARNING: could not read the head of addons (main)"* ]]
    first="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    [[ "$first" == "addons main unknown-"* ]]
    # Never the same value twice: an unknown head is never "unchanged".
    bash "$SCRIPT" record-heads "$DIR" "$TMP/spec" 2>/dev/null
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" != "$first" ]
}

@test "record-heads treats a branch the remote does not have as unknown" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tgone\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_OUT=""
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [[ "$output" == *"WARNING"* ]]
    [[ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" == "addons gone unknown-"* ]]
}

@test "record-heads keeps the file out of the instance's history" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    git init -q "$DIR"
    printf 'pinned\tpin\t%s\t\n' "$PIN_SHA" > "$TMP/spec"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    grep -qxF "/odoo/custom/incubacloud-heads.txt" "$DIR/.git/info/exclude"
    [ -z "$(git -C "$DIR" status --porcelain -- odoo/custom/incubacloud-heads.txt)" ]
}

@test "record-heads writes where the template's .dockerignore lets it in" {
    # doodba's odoo/.dockerignore (template v10.0.8) leaves out
    # custom/src/* but its dotted files, and auto: odoo/custom/ itself
    # reaches ``COPY ./custom``.
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'pinned\tpin\t%s\t\n' "$PIN_SHA" > "$TMP/spec"
    bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ -f "$DIR/odoo/custom/incubacloud-heads.txt" ]
    [ ! -e "$DIR/odoo/custom/src/incubacloud-heads.txt" ]
}

@test "record-heads fails when the build context is not there" {
    printf 'pinned\tpin\t%s\t\n' "$PIN_SHA" > "$TMP/spec"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -ne 0 ]
}

@test "record-heads hands the token to no credential helper of the host" {
    # The SSH user of a host can have a helper that keeps whatever
    # credentials git uses (store, cache, libsecret, a credential
    # manager): the hour's token was found in ~/.git-credentials.
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    git config --global credential.helper store
    # The second URL's credentials are percent-encoded, so they stay in
    # it: git itself has them, and still no helper of the host's.
    printf '%s\tref\tmain\t%s\n' \
        addons "$HEADS_URL" \
        encoded "https://x-access-token:${HEADS_TOKEN}%41@github.com/acme/b.git" \
        > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [[ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" == "addons main $HEADS_SHA"$'\n'"encoded main $HEADS_SHA" ]]
    [ ! -e "$HOME/.git-credentials" ]
}

@test "record-heads keeps the token out of git's command line" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    ! grep -q "$HEADS_TOKEN" "$CALLS"
    basic="$(printf 'x-access-token:%s' "$HEADS_TOKEN" | base64 -w0)"
    [ "$(cat "$CALLS.headers")" = "Authorization: Basic $basic" ]
    # git's own HOME, the header in its config, is gone with the run.
    mkdir "$TMP/tmpdir"
    TMPDIR="$TMP/tmpdir" run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ -z "$(ls -A "$TMP/tmpdir")" ]
}

@test "record-heads sends no credentials to any host but github.com" {
    # A URL that names github.com elsewhere than as its host (after a ?, #
    # or \ that ends the host too), a port, plain http, or credentials git
    # would send as they are: not asked at all, and the build clones it
    # again (5-nov-2026).
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf '%s\tref\tmain\t%s\n' \
        path "https://x-access-token:${HEADS_TOKEN}@evil.example/github.com/x.git" \
        suffix "https://x-access-token:${HEADS_TOKEN}@github.com.evil.example/a.git" \
        port "https://x-access-token:${HEADS_TOKEN}@github.com:8443/acme/a.git" \
        http "http://x-access-token:${HEADS_TOKEN}@github.com/acme/a.git" \
        second "https://x-access-token:${HEADS_TOKEN}@github.com@evil.example/a.git" \
        query "https://x-access-token:${HEADS_TOKEN}@github.com?@evil.example/a.git" \
        hostq "https://x-access-token:${HEADS_TOKEN}@evil.example?@github.com/acme/a.git" \
        hosth "https://x-access-token:${HEADS_TOKEN}@evil.example#@github.com/acme/a.git" \
        hostb "https://x-access-token:${HEADS_TOKEN}@evil.example\\@github.com/acme/a.git" \
        encoded "https://x-access-token:${HEADS_TOKEN}%41@evil.example/a.git" \
        scp "x-access-token:${HEADS_TOKEN}@evil.example:a.git" \
        > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    ! grep -q ls-remote "$CALLS"
    [ ! -s "$CALLS.headers" ]
    [[ "$output" != *"$HEADS_TOKEN"* ]]
    [ "$(grep -c ' main unknown-' "$DIR/odoo/custom/incubacloud-heads.txt")" -eq 11 ]
}

@test "record-heads asks a public URL without any header" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'odoo\tref\t19.0\t%s\n' "https://github.com/odoo/odoo.git" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/19.0
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "odoo 19.0 $HEADS_SHA" ]
    [ ! -s "$CALLS.headers" ]
}

@test "record-heads reads the branch itself, not a ref that ends like it" {
    # Real git, on a repository that has every look-alike of ``main``:
    # a branch under a folder, a tag, and pull request refs.
    remote="$TMP/remote"
    git init -q -b main "$remote"
    git -C "$remote" -c user.name=t -c user.email=t@t commit -q --allow-empty -m main
    main="$(git -C "$remote" rev-parse HEAD)"
    git -C "$remote" -c user.name=t -c user.email=t@t commit -q --allow-empty -m other
    other="$(git -C "$remote" rev-parse HEAD)"
    git -C "$remote" update-ref refs/heads/main "$main"
    git -C "$remote" update-ref refs/heads/x/main "$other"
    git -C "$remote" update-ref refs/pull/1/head "$other"
    git -C "$remote" update-ref refs/pull/1/merge "$other"
    git -C "$remote" tag v1 "$main"
    mkdir -p "$DIR/odoo/custom"
    printf '%s\tref\t%s\tfile://%s\n' \
        addons main "$remote" \
        tagged v1 "$remote" \
        head head "$remote" \
        pr refs/pull/1/head "$remote" > "$TMP/spec"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    heads="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    [[ "$heads" == *"addons main $main"$'\n'* ]]
    [[ "$heads" == *"tagged v1 $main"$'\n'* ]]
    [[ "$heads" == *"head head unknown-"* ]]
    [[ "$heads" == *"pr refs/pull/1/head $other" ]]
}

@test "record-heads asks a branch named like a commit" {
    # ``cafebabe`` and ``20261105`` are branch names too: only the
    # executor knows a ref was pinned, and says so with ``pin``.
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'addons\tref\tcafebabe\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/cafebabe
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "addons cafebabe $HEADS_SHA" ]
    [[ "$(cat "$CALLS")" == *"refs/heads/cafebabe"* ]]
}

@test "record-heads asks for a short while, and no more after one runs out" {
    _stub_git
    _stub_timeout
    mkdir -p "$DIR/odoo/custom"
    printf '%s\tref\tmain\t%s\n' \
        one "$HEADS_URL" two "$HEADS_URL" three "$HEADS_URL" > "$TMP/spec"
    printf 'pinned\tpin\t%s\t\n' "$PIN_SHA" >> "$TMP/spec"
    export LS_REMOTE_HANG=1
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(grep -c '^timeout ' "$CALLS")" -eq 1 ]
    [[ "$(cat "$CALLS")" == *"timeout -k 5 20 git"*"protocol.version=2"* ]]
    [[ "$output" == *"did not arrive within 20s"* ]]
    heads="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    for alias in one two three; do
        [[ "$heads" == *"$alias main unknown-"* ]]
    done
    [[ "$heads" == *"pinned $PIN_SHA $PIN_SHA" ]]
}

@test "record-heads still asks the other servers when one runs out of time" {
    _stub_git
    _stub_timeout
    mkdir -p "$DIR/odoo/custom"
    printf '%s\tref\tmain\t%s\t%s\t\t\n' \
        gl1 "$GL_URL" "$GL_AUTH" gl2 "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    printf '%s\tref\tmain\t%s\n' odoo "$HEADS_URL" addons "$HEADS_URL" >> "$TMP/spec"
    export LS_REMOTE_HANG_ON=git.acme.tld LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(grep -c '^timeout .*git.acme.tld' "$CALLS")" -eq 1 ]
    [[ "$output" == *"the rest of its server are not asked"* ]]
    heads="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    [[ "$heads" == *"gl1 main unknown-"* ]]
    [[ "$heads" == *"gl2 main unknown-"* ]]
    [[ "$heads" == *"odoo main $HEADS_SHA"* ]]
    [[ "$heads" == *"addons main $HEADS_SHA"* ]]
}

@test "record-heads sweeps the temporary HOMEs a killed run on its directory left" {
    _stub_git
    mkdir -p "$DIR/odoo/custom" "$TMP/tmp"
    export TMPDIR="$TMP/tmp"
    token="$(printf '%s' "$DIR" | sha256sum | cut -c1-16)"
    mkdir -p "$TMPDIR/ic-git-$token-dead" "$TMPDIR/ic-git-0123456789abcdef-live"
    echo secret > "$TMPDIR/ic-git-$token-dead/.gitconfig"
    printf 'addons\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ ! -e "$TMPDIR/ic-git-$token-dead" ]
    [ -d "$TMPDIR/ic-git-0123456789abcdef-live" ]
    # Its own are gone too when it ends.
    [ -z "$(find "$TMPDIR" -maxdepth 1 -name "ic-git-$token-*")" ]
}

@test "record-heads also stops asking when git had to be killed (status 137)" {
    _stub_git
    _stub_timeout
    mkdir -p "$DIR/odoo/custom"
    printf '%s\tref\tmain\t%s\n' \
        one "$HEADS_URL" two "$HEADS_URL" three "$HEADS_URL" > "$TMP/spec"
    export LS_REMOTE_HANG=1 LS_REMOTE_HANG_STATUS=137
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(grep -c '^timeout ' "$CALLS")" -eq 1 ]
    [[ "$output" == *"did not arrive within 20s"* ]]
    heads="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    for alias in one two three; do
        [[ "$heads" == *"$alias main unknown-"* ]]
    done
}

@test "debian-archive puts the archive step first in the image's build" {
    mkdir -p "$DIR/odoo/custom/build.d"
    : > "$DIR/odoo/custom/build.d/20-update-pg-repos"
    run bash "$SCRIPT" debian-archive "$DIR"
    [ "$status" -eq 0 ]
    step="$DIR/odoo/custom/build.d/05-incubacloud-debian-archive"
    [ -x "$step" ]
    cmp "$step" "${BATS_TEST_DIRNAME}/../../scripts/doodba/debian_archive.sh"
    [ "$(find "$DIR/odoo/custom/build.d" -type f | sort | head -1)" = "$step" ]
}

@test "debian-archive writes the step again over an older one" {
    mkdir -p "$DIR/odoo/custom/build.d"
    echo "old" > "$DIR/odoo/custom/build.d/05-incubacloud-debian-archive"
    run bash "$SCRIPT" debian-archive "$DIR"
    [ "$status" -eq 0 ]
    cmp "$DIR/odoo/custom/build.d/05-incubacloud-debian-archive" \
        "${BATS_TEST_DIRNAME}/../../scripts/doodba/debian_archive.sh"
}

# ── Git providers other than GitHub and the helper container (8-nov-2026) ──

GL_TOKEN="gldt-tokenOfTheDeploy"
GL_URL="https://gitlab+deploy-token-7:${GL_TOKEN}@git.acme.tld:8443/team/addons.git"
GL_PLAIN="https://git.acme.tld:8443/team/addons.git"
GL_AUTH="git.acme.tld:8443"
HELPER="alpine/git:2.54.0@sha256:0000"

# The host's git, recording also the whole config each ls-remote ran with.
_stub_git_config() {
    _stub_git
    sed -i 's|^\(    "[^"]*" "\${pre\[@\]}"\) config --get-urlmatch http.extraHeader "\$3" >> "\$CALLS.headers"$|&\n\1 config --list >> "$CALLS.config"|' \
        "$TMP/bin/git"
    grep -q 'CALLS.config' "$TMP/bin/git"
}

# A timeout that runs whatever command follows its options.
_stub_timeout_any() {
    cat > "$TMP/bin/timeout" <<'STUB'
#!/usr/bin/env bash
echo "timeout $*" >> "$CALLS"
[ -z "${LS_REMOTE_HANG:-}" ] || exit "${LS_REMOTE_HANG_STATUS:-124}"
[ "$1" != -k ] || shift 2
shift
exec "$@"
STUB
    chmod +x "$TMP/bin/timeout"
}

# docker as the helper container answers it. ``run`` records its argv,
# copies the mounted HOME's .gitconfig to $CALLS.gitconfig.<n>, and
# - for ``--entrypoint git … ls-remote``, prints $LS_REMOTE_OUT;
# - for ``--entrypoint sh … -c <script> sh <url> <target> <expect>``,
#   fails the first $FETCH_FAILS times (status $FETCH_FAIL_STATUS), or
#   writes a tree into the /w mount and prints the commit: <expect>, else
#   $FETCH_SHA. With $FETCH_SLEEP it sleeps first (for the traps).
# ``ps`` prints $DOCKER_PS_IDS; everything else is recorded and succeeds.
_stub_docker_helper() {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$1" in
    ps) [ -z "${DOCKER_PS_IDS:-}" ] || printf '%s\n' $DOCKER_PS_IDS; exit 0 ;;
    rm) [ -z "${LEFTOVER_PROBE:-}" ] || [ ! -e "$LEFTOVER_PROBE" ] \
            || echo "leftover present at docker $*" >> "$CALLS"; exit 0 ;;
    run) ;;
    *) exit 0 ;;
esac
shift
home="" work="" entry=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -v) case "$2" in *:/h:ro) home="${2%:/h:ro}" ;; *:/w) work="${2%:/w}" ;; esac; shift 2 ;;
        --entrypoint) entry="$2"; shift 2 ;;
        --name|--label|--user|-e|-w|--add-host) shift 2 ;;
        --rm) shift ;;
        *) break ;;
    esac
done
shift  # the image
n="$(find "$(dirname "$CALLS")" -maxdepth 1 -name 'calls.gitconfig.*' | wc -l)"
cp "$home/.gitconfig" "$CALLS.gitconfig.$n" 2>/dev/null || : > "$CALLS.gitconfig.$n"
[ ! -f "$home/ca.pem" ] || cp "$home/ca.pem" "$CALLS.ca.$n"
[ -z "${FETCH_SLEEP:-}" ] || sleep "$FETCH_SLEEP"
if [ "$entry" = git ]; then
    printf '%s' "${LS_REMOTE_OUT:-}"
    exit 0
fi
url="$4" target="$5" expect="$6"
[ -z "${EXCLUDE_COPY:-}" ] || cp "$EXCLUDE_COPY" "$CALLS.exclude-at-fetch"
count="$CALLS.fetches"
echo "$url $target" >> "$count"
if [ "$(wc -l < "$count")" -le "${FETCH_FAILS:-0}" ]; then
    echo partial > "$work/half-written"
    exit "${FETCH_FAIL_STATUS:-2}"
fi
echo "tree of $target" > "$work/README"
[ -z "${FETCH_PLANT_LINK:-}" ] || ln -s "$FETCH_PLANT_LINK" "$work/.incubacloud-head"
echo "${expect:-${FETCH_SHA:-4444444444444444444444444444444444444444}}"
STUB
    chmod +x "$TMP/bin/docker"
}

@test "record-heads sends a GitLab token to its own server and port only" {
    _stub_git_config
    mkdir -p "$DIR/odoo/custom"
    printf '%s\tref\tmain\t%s\t%s\t\t\n' \
        good "$GL_URL" "$GL_AUTH" \
        noport "https://gitlab+deploy-token-7:${GL_TOKEN}@git.acme.tld/team/a.git" "$GL_AUTH" \
        other "https://gitlab+deploy-token-7:${GL_TOKEN}@evil.example:8443/team/a.git" "$GL_AUTH" \
        http "http://git.acme.tld:8443/team/a.git" "$GL_AUTH" \
        > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(grep -c ls-remote "$CALLS")" -eq 1 ]
    [[ "$(cat "$CALLS")" == *"ls-remote -- $GL_PLAIN refs/heads/main"* ]]
    ! grep -q "$GL_TOKEN" "$CALLS"
    basic="$(printf 'gitlab+deploy-token-7:%s' "$GL_TOKEN" | base64 -w0)"
    [ "$(cat "$CALLS.headers")" = "Authorization: Basic $basic" ]
    grep -qxF "http.https://$GL_AUTH/.extraheader=Authorization: Basic $basic" "$CALLS.config"
    ! grep -q '^http\.extraheader=' "$CALLS.config"
    heads="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    [[ "$heads" == *"good main $HEADS_SHA"* ]]
    [[ "$heads" == *"noport main unknown-"* ]]
    [[ "$heads" == *"other main unknown-"* ]]
    [[ "$heads" == *"http main unknown-"* ]]
    [[ "$output" != *"$GL_TOKEN"* ]]
}

@test "record-heads refuses redirects and trusts the CA off GitHub only" {
    _stub_git_config
    mkdir -p "$DIR/odoo/custom"
    printf 'pem\n' > "$TMP/ca.pem"
    printf 'gl\tref\tmain\t%s\t%s\t%s\t\n' "$GL_URL" "$GL_AUTH" "$TMP/ca.pem" > "$TMP/spec"
    printf 'gh\tref\tmain\t%s\n' "$HEADS_URL" >> "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ "$(grep -c "^http.https://$GL_AUTH/.followredirects=false$" "$CALLS.config")" -eq 1 ]
    [ "$(grep -c "^http.https://$GL_AUTH/.sslcainfo=$TMP/ca.pem$" "$CALLS.config")" -eq 1 ]
    [ "$(grep -c 'followredirects' "$CALLS.config")" -eq 1 ]
    ! grep -qi curloptresolve "$CALLS.config"
}

@test "record-heads asks a pinned server from the helper container, never the host's git" {
    _stub_git_config
    _stub_timeout_any
    _stub_docker_helper
    mkdir -p "$DIR/odoo/custom"
    printf 'pem\n' > "$TMP/ca.pem"
    printf 'gl\tref\tmain\t%s\t%s\t%s\t%s\n' "$GL_URL" "$GL_AUTH" "$TMP/ca.pem" 203.0.113.7 > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec" "$HELPER"
    [ "$status" -eq 0 ]
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "gl main $HEADS_SHA" ]
    run_line="$(grep '^docker run' "$CALLS")"
    [[ "$run_line" == *"--add-host git.acme.tld:203.0.113.7"* ]]
    [[ "$run_line" == *"--label ic.job="* ]]
    [[ "$run_line" == *":/h:ro"* ]]
    [[ "$run_line" == *"--entrypoint git $HELPER -c credential.helper= -c protocol.version=2 ls-remote -- $GL_PLAIN refs/heads/main refs/tags/main"* ]]
    [[ "$(cat "$CALLS")" == *"timeout -k 5 20 docker run"* ]]
    ! grep -q "$GL_TOKEN" "$CALLS"
    ! grep -q '^git ' "$CALLS"
    grep -q 'sslCAInfo = /h/ca.pem' "$CALLS.gitconfig.0"
    cmp "$CALLS.ca.0" "$TMP/ca.pem"
    ! grep -qi curloptresolve "$CALLS.gitconfig.0"
    # The containers an earlier run on this directory left go first.
    [[ "$(head -1 "$CALLS")" == "docker ps -aq --filter label=ic.job="* ]]
}

@test "record-heads removes the helper container that ran out of time" {
    _stub_timeout_any
    _stub_docker_helper
    mkdir -p "$DIR/odoo/custom"
    printf 'gl\tref\tmain\t%s\t%s\t\t%s\n' "$GL_URL" "$GL_AUTH" 203.0.113.7 > "$TMP/spec"
    export LS_REMOTE_HANG=1
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec" "$HELPER"
    [ "$status" -eq 0 ]
    [[ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" == "gl main unknown-"* ]]
    grep -q '^docker rm -f ic-git-' "$CALLS"
}

@test "record-heads with a pin and no helper image asks nothing" {
    _stub_git
    mkdir -p "$DIR/odoo/custom"
    printf 'gl\tref\tmain\t%s\t%s\t\t%s\n' "$GL_URL" "$GL_AUTH" 203.0.113.7 > "$TMP/spec"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [[ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" == "gl main unknown-"* ]]
    ! grep -q ls-remote "$CALLS"
}

@test "git-trust writes the image's git settings for each Git server, and removes them" {
    mkdir -p "$DIR/odoo/custom"
    git init -q "$DIR"
    printf 'pem-of-acme\n' > "$TMP/ca.pem"
    printf '%s\tref\tmain\t%s\t%s\t%s\t\n' \
        a "$GL_URL" "$GL_AUTH" "$TMP/ca.pem" \
        b "https://oauth2:${GL_TOKEN}@gitlab.com/x/b.git" gitlab.com "" \
        c "https://gitlab.com/x/c.git" gitlab.com "" > "$TMP/spec"
    printf 'gh\tref\tmain\t%s\n' "$HEADS_URL" >> "$TMP/spec"
    run bash "$SCRIPT" git-trust "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    step="$DIR/odoo/custom/build.d/060-incubacloud-git"
    [ -x "$step" ]
    sh -n "$step"
    grep -qxF "git config --system http.https://$GL_AUTH/.followRedirects false" "$step"
    grep -qxF "git config --system http.https://$GL_AUTH/.sslCAInfo /opt/odoo/custom/incubacloud-git/git.acme.tld_8443.pem" "$step"
    grep -qxF "git config --system http.https://gitlab.com/.followRedirects false" "$step"
    [ "$(grep -c 'gitlab.com/' "$step")" -eq 1 ]
    ! grep -q github "$step"
    ! grep -q "$GL_TOKEN" "$step"
    cmp "$DIR/odoo/custom/incubacloud-git/git.acme.tld_8443.pem" "$TMP/ca.pem"
    grep -qxF /odoo/custom/build.d/060-incubacloud-git "$DIR/.git/info/exclude"
    grep -qxF /odoo/custom/incubacloud-git/ "$DIR/.git/info/exclude"
    # With no such server left, nothing stays behind.
    printf 'gh\tref\tmain\t%s\n' "$HEADS_URL" > "$TMP/spec"
    run bash "$SCRIPT" git-trust "$DIR" "$TMP/spec"
    [ "$status" -eq 0 ]
    [ ! -e "$step" ]
    [ ! -e "$DIR/odoo/custom/incubacloud-git" ]
}

@test "git-trust refuses a server name that would break the step" {
    mkdir -p "$DIR/odoo/custom"
    printf 'a\tref\tmain\t%s\t%s\t\t\n' "$GL_PLAIN" 'git.acme.tld;rm' > "$TMP/spec"
    run bash "$SCRIPT" git-trust "$DIR" "$TMP/spec"
    [ "$status" -ne 0 ]
    [ ! -e "$DIR/odoo/custom/build.d/060-incubacloud-git" ]
}

@test "debian-archive installs the ISRG roots step too" {
    mkdir -p "$DIR/odoo/custom/build.d"
    run bash "$SCRIPT" debian-archive "$DIR"
    [ "$status" -eq 0 ]
    step="$DIR/odoo/custom/build.d/06-incubacloud-isrg-roots"
    [ -x "$step" ]
    cmp "$step" "${BATS_TEST_DIRNAME}/../../scripts/doodba/isrg_roots.sh"
}

@test "the ISRG roots step installs both roots, and refuses a certificate that is not theirs" {
    command -v openssl >/dev/null || skip "no openssl"
    cat > "$TMP/bin/update-ca-certificates" <<'STUB'
#!/usr/bin/env bash
echo "update-ca-certificates" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/update-ca-certificates"
    roots="${BATS_TEST_DIRNAME}/../../scripts/doodba/isrg_roots.sh"
    IC_CA_DIR="$TMP/ca" run bash "$roots"
    [ "$status" -eq 0 ]
    [ -f "$TMP/ca/isrg-root-yr.crt" ]
    [ -f "$TMP/ca/isrg-root-ye.crt" ]
    grep -q update-ca-certificates "$CALLS"
    # One character of Root YE changed: its SHA-256 no longer matches.
    sed 's/LTDzGYgtQKO1jl63uAIwGV7m$/LTDzGYgtQKO1jl63uAIwGV7n/' "$roots" > "$TMP/tampered.sh"
    ! cmp -s "$roots" "$TMP/tampered.sh"
    IC_CA_DIR="$TMP/ca2" run bash "$TMP/tampered.sh"
    [ "$status" -ne 0 ]
    [[ "$output" == *"ISRG YE"* ]]
    [ ! -e "$TMP/ca2/isrg-root-ye.crt" ]
}

# An 8.0 instance as fetch-repos finds it, a git repository with the
# template's .dockerignore.
_instance_80() {
    mkdir -p "$DIR/odoo/custom/src/private"
    git init -q "$DIR"
    printf '# Ignore all src directories except private and files with a dot\ncustom/src/*\n!custom/src/private\n!custom/src/*.*\nauto\n' \
        > "$DIR/odoo/.dockerignore"
    _stub_timeout_any
    _stub_docker_helper
}

_spec_80() {
    printf 'odoo\tref\t8.0\thttps://github.com/odoo/odoo.git\t\t\t\n' > "$TMP/spec"
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" >> "$TMP/spec"
    printf 'priv\tpin\t%s\t%s\t\t\t\n' "$PIN_SHA" "$HEADS_URL" >> "$TMP/spec"
}

@test "fetch-repos fetches every repository at its head, out of git and into the build" {
    _instance_80
    _spec_80
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
$HEADS_SHA	refs/heads/8.0
" FETCH_SHA="$HEADS_SHA"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    src="$DIR/odoo/custom/src"
    for alias in odoo addons priv; do
        [ -f "$src/$alias/README" ]
        [ ! -e "$src/$alias/.git" ]
    done
    [ "$(cat "$src/.ic-heads/addons")" = "$HEADS_SHA" ]
    [ "$(cat "$src/.ic-heads/priv")" = "$PIN_SHA" ]
    [ ! -e "$src/addons/.incubacloud-head" ]
    [ -z "$(find "$src" -maxdepth 1 -name '.ic-tmp-*')" ]
    # The pinned one is fetched by its commit, the others by their head.
    grep -qx "https://github.com/acme/addons.git $PIN_SHA" "$CALLS.fetches"
    grep -qx "$GL_PLAIN $HEADS_SHA" "$CALLS.fetches"
    # Nothing of the tokens on any command line or in the output.
    ! grep -q "$GL_TOKEN" "$CALLS"
    ! grep -q "$HEADS_TOKEN" "$CALLS"
    [[ "$output" != *"$GL_TOKEN"* && "$output" != *"$HEADS_TOKEN"* ]]
    # Every container refused redirects, and none saw both tokens.
    gl_basic="$(printf 'gitlab+deploy-token-7:%s' "$GL_TOKEN" | base64 -w0)"
    gh_basic="$(printf 'x-access-token:%s' "$HEADS_TOKEN" | base64 -w0)"
    for cfg in "$CALLS".gitconfig.*; do
        grep -q 'followRedirects = false' "$cfg"
        # The header only for the server it is for, never global.
        [ -z "$(git config -f "$cfg" --get-all http.extraHeader)" ]
        ! { grep -q "$gl_basic" "$cfg" && grep -q "$gh_basic" "$cfg"; }
    done
    grep -lq "$gl_basic" "$CALLS".gitconfig.*
    grep -lq "$gh_basic" "$CALLS".gitconfig.*
    # The build lets the trees in, and keeps the temporary ones out.
    ignore="$DIR/odoo/Dockerfile.dockerignore"
    head -5 "$ignore" | cmp - "$DIR/odoo/.dockerignore"
    for alias in odoo addons priv; do grep -qxF "!custom/src/$alias" "$ignore"; done
    [ "$(tail -1 "$ignore")" = 'custom/src/.ic-*' ]
    # Out of the instance's history.
    for line in /odoo/custom/src/odoo/ /odoo/custom/src/addons/ /odoo/custom/src/priv/ \
        '/odoo/custom/src/.ic-*' /odoo/Dockerfile.dockerignore; do
        grep -qxF "$line" "$DIR/.git/info/exclude"
    done
    [ -z "$(git -C "$DIR" status --porcelain --untracked-files=all -- odoo/custom/src odoo/Dockerfile.dockerignore)" ]
    heads="$(cat "$DIR/odoo/custom/incubacloud-heads.txt")"
    [[ "$heads" == *"addons main $HEADS_SHA"* ]]
}

@test "fetch-repos writes the exclusions before the first download" {
    _instance_80
    _spec_80
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" EXCLUDE_COPY="$DIR/.git/info/exclude"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    grep -qxF /odoo/custom/src/addons/ "$CALLS.exclude-at-fetch"
    grep -qxF '/odoo/custom/src/.ic-*' "$CALLS.exclude-at-fetch"
    grep -qxF /odoo/Dockerfile.dockerignore "$CALLS.exclude-at-fetch"
}

@test "fetch-repos does not fetch a tree already at its head" {
    _instance_80
    _spec_80
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
$HEADS_SHA	refs/heads/8.0
" FETCH_SHA="$HEADS_SHA"
    bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    : > "$CALLS.fetches"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ ! -s "$CALLS.fetches" ]
    [[ "$output" == *"addons is at $HEADS_SHA already"* ]]
    [[ "$output" == *"3 repositories ready for the build, 0 fetched"* ]]
}

@test "fetch-repos keeps the old tree when the download fails, and retries once" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    mkdir -p "$DIR/odoo/custom/src/addons"
    echo old > "$DIR/odoo/custom/src/addons/README"
    mkdir -p "$DIR/odoo/custom/src/.ic-heads"
    echo 1111 > "$DIR/odoo/custom/src/.ic-heads/addons"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" FETCH_FAILS=2 FETCH_FAIL_STATUS=137
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -ne 0 ]
    [ "$(wc -l < "$CALLS.fetches")" -eq 2 ]
    [[ "$output" == *"addons: the download failed twice (status 137)"* ]]
    [[ "$output" != *"git.acme.tld"* ]]
    [ "$(cat "$DIR/odoo/custom/src/addons/README")" = old ]
    [ "$(cat "$DIR/odoo/custom/src/.ic-heads/addons")" = 1111 ]
    [ -z "$(find "$DIR/odoo/custom/src" -maxdepth 1 -name '.ic-tmp-*')" ]
    grep -q '^docker rm -f ic-git-' "$CALLS"
    # One failure, then a good download: it ends well.
    : > "$CALLS.fetches"
    export FETCH_FAILS=1
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"trying again in 0s"* ]]
    [ "$(cat "$DIR/odoo/custom/src/.ic-heads/addons")" = "$HEADS_SHA" ]
    [ ! -e "$DIR/odoo/custom/src/addons/half-written" ]
}

@test "fetch-repos starts over from what a killed run left" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    # A run killed mid-download: a partial temporary tree, no marker, and
    # its container still up.
    mkdir -p "$DIR/odoo/custom/src/.ic-tmp-addons"
    echo partial > "$DIR/odoo/custom/src/.ic-tmp-addons/half-written"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" DOCKER_PS_IDS="c0ffee"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    grep -q '^docker rm -f c0ffee$' "$CALLS"
    [ ! -e "$DIR/odoo/custom/src/.ic-tmp-addons" ]
    [ ! -e "$DIR/odoo/custom/src/addons/half-written" ]
    [ "$(cat "$DIR/odoo/custom/src/.ic-heads/addons")" = "$HEADS_SHA" ]
}

@test "fetch-repos removes the container in flight when the job is cancelled" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    for signal in TERM HUP; do
        : > "$CALLS"
        FETCH_SLEEP=3 bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0 \
            >/dev/null 2>&1 &
        pid=$!
        for _ in $(seq 100); do
            [ "$(grep -c 'entrypoint sh' "$CALLS")" -lt 1 ] || break
            sleep 0.1
        done
        kill -s "$signal" "$pid"
        status=0
        wait "$pid" || status=$?
        [ "$status" -ne 0 ]
        grep -q '^docker rm -f ic-git-' "$CALLS"
        [ ! -e "$DIR/odoo/custom/src/.ic-heads/addons" ]
    done
}

@test "fetch-repos fetches a branch whose head could not be read by its name" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    export LS_REMOTE_OUT="" FETCH_SHA="5555555555555555555555555555555555555555"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    grep -qx "$GL_PLAIN main" "$CALLS.fetches"
    [ "$(cat "$DIR/odoo/custom/src/.ic-heads/addons")" = "$FETCH_SHA" ]
    [ "$(cat "$DIR/odoo/custom/incubacloud-heads.txt")" = "addons main $FETCH_SHA" ]
}

@test "fetch-repos refuses a repository with several merges" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -ne 0 ]
    [[ "$output" == *"addons has several merges"* ]]
    ! grep -q '^docker run' "$CALLS"
}

@test "fetch-repos fails, naming the alias, when the server redirects" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" FETCH_FAILS=1 FETCH_FAIL_STATUS=4
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -ne 0 ]
    [[ "$output" == *"addons: the server redirects; update the repository's URL"* ]]
}

@test "fetch-repos removes the trees of repositories the instance dropped" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    mkdir -p "$DIR/odoo/custom/src/gone" "$DIR/odoo/custom/src/mine" \
        "$DIR/odoo/custom/src/.ic-heads"
    echo 1111 > "$DIR/odoo/custom/src/.ic-heads/gone"
    # A marker of the repository's own does not make a tree the step's.
    echo 1111 > "$DIR/odoo/custom/src/mine/.incubacloud-head"
    echo kept > "$DIR/odoo/custom/src/mine/x"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ ! -e "$DIR/odoo/custom/src/gone" ]
    [ ! -e "$DIR/odoo/custom/src/.ic-heads/gone" ]
    [ -f "$DIR/odoo/custom/src/mine/x" ]
    [ -d "$DIR/odoo/custom/src/private" ]
}

@test "fetch-repos pins a server's address in its container" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t%s\n' "$GL_URL" "$GL_AUTH" 203.0.113.7 > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ "$(grep '^docker run' "$CALLS" | grep -c -- '--add-host git.acme.tld:203.0.113.7')" -eq 2 ]
    [[ "$(cat "$CALLS")" == *"timeout -k 5 600 docker run"* ]]
}

@test "fetch-repos never writes through a link the repository plants" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    echo untouched > "$TMP/victim"
    mkdir -p "$TMP/victim-dir"
    # The instance's own tree may carry a link where the markers go, too.
    ln -s "$TMP/victim-dir" "$DIR/odoo/custom/src/.ic-heads"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" FETCH_PLANT_LINK="$TMP/victim"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/victim")" = untouched ]
    [ -z "$(ls -A "$TMP/victim-dir")" ]
    [ -L "$DIR/odoo/custom/src/addons/.incubacloud-head" ]
    [ ! -L "$DIR/odoo/custom/src/.ic-heads" ]
    [ "$(cat "$DIR/odoo/custom/src/.ic-heads/addons")" = "$HEADS_SHA" ]
    # A second run reads no marker through the planted link either.
    : > "$CALLS.fetches"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ ! -s "$CALLS.fetches" ]
    [ "$(cat "$TMP/victim")" = untouched ]
}

@test "fetch-repos keeps the markers out of the build and of the instance's history" {
    _instance_80
    _spec_80
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
$HEADS_SHA	refs/heads/8.0
" FETCH_SHA="$HEADS_SHA"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ -d "$DIR/odoo/custom/src/.ic-heads" ]
    [ -z "$(git -C "$DIR" status --porcelain --untracked-files=all -- odoo/custom/src)" ]
    grep -qxF 'custom/src/.ic-*' "$DIR/odoo/Dockerfile.dockerignore"
}

@test "fetch-repos stops at once, saying why, on a URL whose credentials are refused" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' \
        "https://gitlab+deploy-token-7:${GL_TOKEN}@evil.example:8443/team/a.git" "$GL_AUTH" \
        > "$TMP/spec"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -ne 0 ]
    [[ "$output" == *"addons: its URL is refused"* ]]
    [[ "$output" != *"failed twice"* && "$output" != *"trying again"* ]]
    [[ "$output" != *"$GL_TOKEN"* ]]
    [ ! -s "$CALLS.fetches" ]
}

@test "fetch-repos removes a killed run's containers before its temporary trees" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    mkdir -p "$DIR/odoo/custom/src/.ic-tmp-addons"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" DOCKER_PS_IDS="c0ffee" LEFTOVER_PROBE="$DIR/odoo/custom/src/.ic-tmp-addons"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    grep -q '^leftover present at docker rm -f c0ffee$' "$CALLS"
}

@test "record-heads removes a killed run's containers before its temporary HOMEs" {
    _stub_timeout_any
    _stub_docker_helper
    mkdir -p "$DIR/odoo/custom" "$TMP/tmp"
    export TMPDIR="$TMP/tmp"
    token="$(printf '%s' "$DIR" | sha256sum | cut -c1-16)"
    mkdir -p "$TMPDIR/ic-git-$token-dead"
    printf 'gl\tref\tmain\t%s\t%s\t\t%s\n' "$GL_URL" "$GL_AUTH" 203.0.113.7 > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" DOCKER_PS_IDS="c0ffee" LEFTOVER_PROBE="$TMPDIR/ic-git-$token-dead"
    run bash "$SCRIPT" record-heads "$DIR" "$TMP/spec" "$HELPER"
    [ "$status" -eq 0 ]
    grep -q '^leftover present at docker rm -f c0ffee$' "$CALLS"
    [ ! -e "$TMPDIR/ic-git-$token-dead" ]
}

@test "fetch-repos removes a killed run's containers before its temporary HOMEs" {
    _instance_80
    mkdir -p "$TMP/tmp"
    export TMPDIR="$TMP/tmp"
    token="$(printf '%s' "$DIR" | sha256sum | cut -c1-16)"
    mkdir -p "$TMPDIR/ic-git-$token-dead"
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" DOCKER_PS_IDS="c0ffee" LEFTOVER_PROBE="$TMPDIR/ic-git-$token-dead"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    grep -q '^leftover present at docker rm -f c0ffee$' "$CALLS"
    [ ! -e "$TMPDIR/ic-git-$token-dead" ]
}

@test "fetch-repos removes at its start the markers' temporary files a killed run left" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    mkdir -p "$DIR/odoo/custom/src/.ic-heads"
    echo 1111 > "$DIR/odoo/custom/src/.ic-heads/.tmp-dead1234"
    # Even a run that fails afterwards.
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
" FETCH_FAILS=2
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -ne 0 ]
    [ ! -e "$DIR/odoo/custom/src/.ic-heads/.tmp-dead1234" ]
    : > "$CALLS.fetches"
    echo 1111 > "$DIR/odoo/custom/src/.ic-heads/.tmp-dead5678"
    export FETCH_FAILS=0
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ -z "$(find "$DIR/odoo/custom/src/.ic-heads" -name '.tmp-*')" ]
    [ "$(cat "$DIR/odoo/custom/src/.ic-heads/addons")" = "$HEADS_SHA" ]
}

@test "fetch-repos removes no file through a markers directory that is a link" {
    _instance_80
    printf 'addons\tref\tmain\t%s\t%s\t\t\n' "$GL_URL" "$GL_AUTH" > "$TMP/spec"
    src="$DIR/odoo/custom/src"
    # The instance's tree carries .ic-heads as a link to a directory
    # whose files look like markers: one at the head of addons, so its
    # tree is not fetched again, and one of an alias it does not have.
    mkdir -p "$TMP/victim-dir" "$src/addons" "$src/gone"
    echo "$HEADS_SHA" > "$TMP/victim-dir/addons"
    echo kept > "$TMP/victim-dir/gone"
    echo kept > "$src/gone/x"
    ln -s "$TMP/victim-dir" "$src/.ic-heads"
    export LS_REMOTE_OUT="$HEADS_SHA	refs/heads/main
"
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/victim-dir/gone")" = kept ]
    [ "$(cat "$TMP/victim-dir/addons")" = "$HEADS_SHA" ]
    [ -f "$src/gone/x" ]
    [ ! -L "$src/.ic-heads" ]
    [ "$(cat "$src/.ic-heads/addons")" = "$HEADS_SHA" ]
}

@test "fetch-repos refuses an alias doodba keeps for itself in custom/src" {
    _instance_80
    src="$DIR/odoo/custom/src"
    for alias in repos.yaml repos.yml addons.yaml addons.yml private; do
        : > "$CALLS"
        [ -d "$src/$alias" ] || echo "own $alias" > "$src/$alias"
        printf '%s\tref\tmain\t%s\t%s\t\t\n' "$alias" "$GL_URL" "$GL_AUTH" > "$TMP/spec"
        run bash "$SCRIPT" fetch-repos "$DIR" "$TMP/spec" "$HELPER" 0
        [ "$status" -ne 0 ]
        [[ "$output" == *"invalid repository alias: $alias"* ]]
        ! grep -q '^docker run' "$CALLS"
        [ -d "$src/$alias" ] || [ "$(cat "$src/$alias")" = "own $alias" ]
    done
}
