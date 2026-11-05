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
    # The name QA's walk 6g2 deployed on 10-oct-2026: 68 bytes.
    long="qa-addons-privado-production.vps.qa-20261010-2457-pago.incubacloud.io"
    printf 'services:\n  odoo:\n    hostname: "%s"\n' "$long" > "$DIR/prod.yaml"
    run bash "$SCRIPT" cap-hostnames "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *"capped hostname in prod.yaml"* ]]
    grep -q '    hostname: "qa-addons-privado-production.vps.qa-20261010-2457-pago"$' \
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
    "$real" "\${pre[@]}" config --get-all http.extraHeader >> "\$CALLS.headers"
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
# of time (status 124, nothing run) when $LS_REMOTE_HANG is set.
_stub_timeout() {
    cat > "$TMP/bin/timeout" <<'STUB'
#!/usr/bin/env bash
echo "timeout $*" >> "$CALLS"
[ -z "${LS_REMOTE_HANG:-}" ] || exit 124
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
