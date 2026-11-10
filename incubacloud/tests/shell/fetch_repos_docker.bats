#!/usr/bin/env bats
# deploy.sh fetch-repos with a real Docker and the pinned helper image
# (the executor's GIT_HELPER_IMAGE), against a Git server over https of
# the test's own (fixtures/git_https_server.py) that logs whether each
# request carried a credential. Skipped without Docker, openssl, git or
# python3, and without the helper image unless IC_PULL_HELPER is set:
# it weighs hundreds of MB.

# Print the executor's GIT_HELPER_IMAGE.
_helper_image() {
    grep -A3 '^GIT_HELPER_IMAGE = (' \
        "${BATS_TEST_DIRNAME}/../../models/deploy_instance_executor.py" \
        | grep -o '"[^"]*"' | tr -d '"\n'
}

setup_file() {
    for tool in docker openssl git python3; do
        command -v "$tool" >/dev/null || skip "no $tool"
    done
    docker info >/dev/null 2>&1 || skip "no Docker daemon"
    IMAGE="$(_helper_image)"
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        [ -n "${IC_PULL_HELPER:-}" ] || skip "the helper image is not here (IC_PULL_HELPER=1 pulls it)"
        docker pull -q "$IMAGE" >/dev/null || skip "the helper image could not be pulled"
    fi
    GATEWAY="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null)"
    [ -n "$GATEWAY" ] || skip "no gateway on Docker's bridge"
    FILE_TMP="$(mktemp -d)"
    pki="$FILE_TMP/pki"
    mkdir -p "$pki" "$FILE_TMP/repos/team"
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$pki/ca.key" \
        -out "$pki/ca.pem" -days 1 -subj "/CN=fetch-repos test CA" 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -keyout "$pki/srv.key" \
        -out "$pki/srv.csr" -subj "/CN=gitsrv.test" 2>/dev/null
    printf 'subjectAltName=DNS:gitsrv.test\n' > "$pki/ext"
    openssl x509 -req -in "$pki/srv.csr" -CA "$pki/ca.pem" -CAkey "$pki/ca.key" \
        -CAcreateserial -out "$pki/srv.pem" -days 1 -extfile "$pki/ext" 2>/dev/null
    # addons: main and a feature branch, each with a file of its own and a
    # 4 MB one, so a layer that copied the tree again would show; other.
    for repo in addons other; do
        work="$FILE_TMP/work-$repo"
        git init -q -b main "$work"
        echo "# $repo" > "$work/README.md"
        mkdir -p "$work/mod_$repo"
        echo "{'name': '$repo'}" > "$work/mod_$repo/__manifest__.py"
        printf '#!/bin/sh\n' > "$work/run.sh"
        chmod +x "$work/run.sh"
        head -c 4194304 /dev/urandom > "$work/blob.bin"
        git -C "$work" add -A
        git -C "$work" -c user.name=t -c user.email=t@t commit -qm init
    done
    git -C "$FILE_TMP/work-addons" checkout -q -b feature
    echo feature > "$FILE_TMP/work-addons/feature.txt"
    git -C "$FILE_TMP/work-addons" add -A
    git -C "$FILE_TMP/work-addons" -c user.name=t -c user.email=t@t commit -qm feature
    for repo in addons other; do
        git clone -q --bare "$FILE_TMP/work-$repo" "$FILE_TMP/repos/team/$repo.git"
    done
    PORT="$((20000 + RANDOM % 20000))"
    : > "$FILE_TMP/access.log"
    python3 "${BATS_TEST_DIRNAME}/fixtures/git_https_server.py" "$FILE_TMP/repos" \
        "$FILE_TMP/access.log" "$pki/srv.pem" "$pki/srv.key" "$PORT" \
        >/dev/null 2>&1 &
    echo "$!" > "$FILE_TMP/server.pid"
    for _ in $(seq 50); do
        if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
            break
        fi
        sleep 0.1
    done
    export IMAGE GATEWAY FILE_TMP PORT
}

teardown_file() {
    [ -n "${FILE_TMP:-}" ] || return 0
    kill "$(cat "$FILE_TMP/server.pid")" 2>/dev/null || true
    docker image rm -f ic-fetch-test:built ic-fetch-test:sleep >/dev/null 2>&1 || true
    rm -rf "$FILE_TMP"
}

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/deploy.sh"
    export HOME="$BATS_TEST_TMPDIR/home"
    export TMPDIR="$BATS_TEST_TMPDIR/tmp"
    DIR="$HOME/projects/inst"
    mkdir -p "$TMPDIR" "$DIR/odoo/custom/src/private"
    printf 'custom/src/*\n!custom/src/private\n!custom/src/*.*\nauto\n' > "$DIR/odoo/.dockerignore"
    SERVER="gitsrv.test:$PORT"
    BASE="https://$SERVER/team"
    TOKEN="testtoken-$RANDOM$RANDOM"
    : > "$FILE_TMP/access.log"
}

# The panel's repos.yaml for <alias>=<url> [<ref>...] lines, one per
# argument; with several refs at doodba's depth for merges, as a shallow
# clone of one commit has no history to merge with.
_repos_yaml() {
    local line alias url refs depth
    for line in "$@"; do
        alias="${line%%=*}"
        url="${line#*=}"
        refs="${url#* }"
        url="${url%% *}"
        # shellcheck disable=SC2016  # doodba's variables, expanded by gitaggregate
        depth='$DEPTH_DEFAULT'
        # shellcheck disable=SC2016
        [[ "$refs" != *" "* ]] || depth='$DEPTH_MERGE'
        printf '%s:\n  defaults:\n    depth: %s\n  remotes:\n    %s: %s\n  merges:\n' \
            "$alias" "$depth" "$alias" "$url"
        for ref in $refs; do
            printf '  - %s %s\n' "$alias" "$ref"
        done
        printf '  target: %s main\n' "$alias"
    done > "$DIR/odoo/custom/src/repos.yaml"
}

# One spec line: <alias> <ref> <url>, with the test server's columns.
_spec_line() {
    printf '%s\tref\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$SERVER" \
        "$FILE_TMP/pki/ca.pem" "$GATEWAY"
}

_fetch() {
    run bash "$SCRIPT" fetch-repos "$DIR" "$TMPDIR/spec" "$IMAGE" 19.0 0 300
}

@test "the pinned helper has git 2.39.5 or newer and git-aggregator 4.0" {
    run docker run --rm --entrypoint sh "$IMAGE" -c \
        'git --version; pip show git-aggregator | sed -n "s/^Version: //p"'
    [ "$status" -eq 0 ]
    git_version="$(sed -n 's/^git version //p' <<< "$output")"
    [ "$(printf '2.39.5\n%s\n' "$git_version" | sort -V | head -1)" = 2.39.5 ]
    [ "$(tail -1 <<< "$output")" = 4.0 ]
}

@test "fetch-repos downloads each tree without .git, in the modes doodba keeps, the credential only to its repository" {
    _repos_yaml "addons=$BASE/addons.git main" "other=$BASE/other.git main"
    {
        _spec_line addons main "https://ictest:$TOKEN@$SERVER/team/addons.git"
        _spec_line other main "$BASE/other.git"
    } > "$TMPDIR/spec"
    _fetch
    [ "$status" -eq 0 ]
    src="$DIR/odoo/custom/src"
    for alias in addons other; do
        [ -f "$src/$alias/README.md" ]
        [ ! -e "$src/$alias/.git" ]
    done
    [ -z "$(find "$src/addons" "$src/other" -type f ! -perm 0640 ! -perm 0750)" ]
    [ -z "$(find "$src/addons" "$src/other" -type d ! -perm 0750)" ]
    [ "$(stat -c %a "$src/addons/run.sh")" = 750 ]
    grep -q '^GET /team/addons.git/info/refs auth=yes$' "$FILE_TMP/access.log"
    ! grep -q '^[A-Z]* /team/addons.git/.* auth=no$' "$FILE_TMP/access.log" || false
    grep -q '^GET /team/other.git/info/refs auth=no$' "$FILE_TMP/access.log"
    ! grep -q '^[A-Z]* /team/other.git/.* auth=yes$' "$FILE_TMP/access.log" || false
    # No credential is left on the host: the spec went with the step.
    [ ! -e "$TMPDIR/spec" ]
    [ -z "$(grep -rl "$TOKEN" "$HOME" "$TMPDIR")" ]
    [[ "$output" != *"$TOKEN"* ]]
}

@test "two merges of one repository make the merged tree" {
    _repos_yaml "addons=$BASE/addons.git main feature"
    {
        _spec_line addons main "$BASE/addons.git"
        _spec_line addons feature "$BASE/addons.git"
    } > "$TMPDIR/spec"
    _fetch
    [ "$status" -eq 0 ]
    [ -f "$DIR/odoo/custom/src/addons/feature.txt" ]
    [ -f "$DIR/odoo/custom/src/addons/README.md" ]
    [ ! -e "$DIR/odoo/custom/src/addons/.git" ]
    [ "$(wc -c < "$DIR/odoo/custom/src/.ic-heads/addons")" -eq 65 ]
}

@test "a URL that redirects fails, saying so" {
    _repos_yaml "other=https://$SERVER/moved/team/other.git main"
    _spec_line other main "https://$SERVER/moved/team/other.git" > "$TMPDIR/spec"
    _fetch
    [ "$status" -ne 0 ]
    [[ "$output" == *"other: the server redirects; update the repository's URL"* ]]
    ! grep -q '^GET /team/other.git' "$FILE_TMP/access.log" || false
}

@test "an image built with AGGREGATE=false holds no .git or credential, and its RUN does not copy the tree again" {
    _repos_yaml "addons=$BASE/addons.git main"
    _spec_line addons main "https://ictest:$TOKEN@$SERVER/team/addons.git" > "$TMPDIR/spec"
    _fetch
    [ "$status" -eq 0 ]
    # The image's own steps that matter here: doodba's COPY of custom/,
    # then its 800-permissions, which changes only what lacks its modes.
    cat > "$DIR/odoo/Dockerfile" <<EOF
FROM $IMAGE
ARG AGGREGATE=true
RUN (getent group odoo || groupadd odoo) && (id odoo || useradd -g odoo odoo)
COPY --chown=root:odoo custom /opt/odoo/custom
RUN python3 /opt/odoo/common/build.d/800-permissions
EOF
    run docker build --progress plain --build-arg AGGREGATE=false \
        -t ic-fetch-test:built "$DIR/odoo"
    [ "$status" -eq 0 ]
    [[ "$output" != *"$TOKEN"* ]]
    [[ "$output" != *"$(printf 'ictest:%s' "$TOKEN" | base64 -w0)"* ]]
    run docker run --rm --entrypoint sh ic-fetch-test:built -c \
        'find /opt/odoo/custom -name .git; grep -c "://[^/]*@" /opt/odoo/custom/src/repos.yaml; ls /opt/odoo/custom/src/addons'
    [[ "$output" != *".git"* ]]
    [[ "$output" == *"0"* ]]
    [[ "$output" == *"blob.bin"* ]]
    ! docker history --no-trunc ic-fetch-test:built | grep -q "$TOKEN" || false
    # The last RUN, 800-permissions, holds nothing of the 4 MB tree.
    run_size="$(docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' ic-fetch-test:built \
        | grep '800-permissions' | cut -f1)"
    [[ "$run_size" =~ ^[0-9.]+(B|kB)$ ]]
}

@test "emptying the record of builds while one runs does not break it" {
    docker buildx history --help >/dev/null 2>&1 || skip "no docker buildx history"
    mkdir -p "$BATS_TEST_TMPDIR/ctx"
    printf 'FROM %s\nRUN sleep 6\n' "$IMAGE" > "$BATS_TEST_TMPDIR/ctx/Dockerfile"
    docker build --no-cache -q -t ic-fetch-test:sleep "$BATS_TEST_TMPDIR/ctx" \
        > "$BATS_TEST_TMPDIR/build.out" 2>&1 &
    pid=$!
    sleep 3
    docker buildx history rm --all >/dev/null 2>&1 || true
    status=0
    wait "$pid" || status=$?
    [ "$status" -eq 0 ]
    docker image inspect ic-fetch-test:sleep >/dev/null
}
