#!/usr/bin/env bats
# The development copy is accepted by Compose as it is exported.
#
# export_sanitize.bats checks what the archive holds against a stand-in
# devel.yaml. This renders the project as the deploy does — the pinned
# doodba template, the answers the panel emits — exports it with the real
# script and runs ``docker compose config`` on the copy, so a template whose
# development stack reads a file the export leaves out (an env file, as on
# 12-oct-2026) fails here. Needs copier, Docker Compose and the network:
# the template comes from GitHub. Fails without them, saying which: a
# check that skips quietly is one nobody notices is not running.

# Fail the test, naming what is missing.
missing() {
    echo "$1" >&2
    return 1
}

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/export_sanitize.sh"
    FIXTURES="${BATS_TEST_DIRNAME}/fixtures"
    command -v copier >/dev/null || missing "copier is needed (pipx install copier)"
    command -v rsync >/dev/null || missing "rsync is needed"
    docker compose version >/dev/null 2>&1 || missing "Docker Compose is needed"
    git ls-remote https://github.com/Tecnativa/doodba-copier-template HEAD \
        >/dev/null 2>&1 || missing "GitHub is not reachable"

    TMP="$(mktemp -d)"
    INST="$TMP/acme-prod"
    OUT="$TMP/acme-prod-export"
    mkdir -p "$OUT"
}

teardown() {
    rm -rf "$TMP"
}

@test "a project rendered from the pinned template, once exported, passes compose config" {
    # Tasks skipped: they set up git and pre-commit, nothing the export reads.
    run copier copy --defaults --overwrite --trust --skip-tasks \
        --vcs-ref "$(cat "$FIXTURES/doodba_template_ref")" \
        --data-file "$FIXTURES/export_copier_answers.yml" \
        gh:Tecnativa/doodba-copier-template "$INST"
    [ "$status" -eq 0 ]
    # The deploy points a production instance at its production stack.
    ln -sfn prod.yaml "$INST/docker-compose.yml"

    run bash "$SCRIPT" "$INST" "$TMP/staging" "$TMP/export.tar.gz"
    [ "$status" -eq 0 ]
    tar -xzf "$TMP/export.tar.gz" -C "$OUT"
    [ "$(readlink "$OUT/docker-compose.yml")" = "devel.yaml" ]

    cd "$OUT"
    run docker compose config -q
    echo "$output"
    [ "$status" -eq 0 ]
}
