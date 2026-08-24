#!/usr/bin/env bats
# Tests for scripts/lib/common.sh.
#
# Run locally with:  bats incubacloud/tests/shell
# CI runs the same command in the "Shell tests (bats)" job.

setup() {
    # shellcheck source=../../scripts/lib/common.sh
    source "${BATS_TEST_DIRNAME}/../../scripts/lib/common.sh"
    TMP="$(mktemp -d)"
}

teardown() {
    rm -rf "$TMP"
}

# Build a copier-shaped instance repo with the live log tracked, which
# is the state every instance deployed before this fix is already in.
_repo_with_tracked_log() {
    local dir="$TMP/inst"
    mkdir -p "$dir/logs"
    git -C "$dir" init -q
    git -C "$dir" config user.email t@example.com
    git -C "$dir" config user.name Test
    printf 'template\n' > "$dir/prod.yaml"
    printf 'boot line\n' > "$dir/logs/odoo.log"
    git -C "$dir" add -A
    git -C "$dir" commit -qm initial
    printf '%s\n' "$dir"
}

@test "ic_log prefixes the message" {
    run ic_log "deploying instance"
    [ "$status" -eq 0 ]
    [ "$output" = "[incubacloud] deploying instance" ]
}

@test "ic_log joins its arguments" {
    run ic_log one two three
    [ "$output" = "[incubacloud] one two three" ]
}

@test "ic_warn marks the line as a warning and keeps going" {
    run ic_warn "image is stale"
    [ "$status" -eq 0 ]
    [ "$output" = "[incubacloud] WARNING: image is stale" ]
}

@test "ic_die exits non-zero with an ERROR line" {
    run ic_die "compose file missing"
    [ "$status" -eq 1 ]
    [ "$output" = "[incubacloud] ERROR: compose file missing" ]
}

@test "ic_expand_home expands a leading tilde against \$HOME" {
    HOME=/home/cloud run ic_expand_home "~/project/inst"
    [ "$output" = "/home/cloud/project/inst" ]
}

@test "ic_expand_home expands a bare tilde" {
    HOME=/home/cloud run ic_expand_home "~"
    [ "$output" = "/home/cloud" ]
}

@test "ic_expand_home leaves an absolute path alone" {
    HOME=/home/cloud run ic_expand_home "/srv/odoo/inst"
    [ "$output" = "/srv/odoo/inst" ]
}

@test "ic_expand_home only expands a tilde at the start" {
    HOME=/home/cloud run ic_expand_home "/srv/~/inst"
    [ "$output" = "/srv/~/inst" ]
}

@test "ic_require_args passes when enough arguments were given" {
    run ic_require_args 2 3 "script.sh <dir> <name>"
    [ "$status" -eq 0 ]
    [ "$output" = "" ]
}

@test "ic_require_args passes on the exact count" {
    run ic_require_args 2 2 "script.sh <dir> <name>"
    [ "$status" -eq 0 ]
}

@test "ic_require_args aborts and shows the usage line" {
    run ic_require_args 2 1 "script.sh <dir> <name>"
    [ "$status" -eq 1 ]
    [[ "$output" == *"expected at least 2 argument(s), got 1."* ]]
    [[ "$output" == *"Usage: script.sh <dir> <name>"* ]]
}

@test "ic_require_args aborts without a usage line when none is given" {
    run ic_require_args 1 0
    [ "$status" -eq 1 ]
    [[ "$output" == *"expected at least 1 argument(s), got 0."* ]]
    [[ "$output" != *"Usage:"* ]]
}

@test "ic_git_exclude_logs excludes the log directory" {
    dir="$(_repo_with_tracked_log)"
    run ic_git_exclude_logs "$dir"
    [ "$status" -eq 0 ]
    grep -qxF '/logs/' "$dir/.git/info/exclude"
}

@test "ic_git_exclude_logs untracks the log but leaves it on disk" {
    dir="$(_repo_with_tracked_log)"
    ic_git_exclude_logs "$dir"
    [ -z "$(git -C "$dir" ls-files logs)" ]
    [ -f "$dir/logs/odoo.log" ]
}

@test "ic_git_exclude_logs leaves the tree clean while Odoo keeps writing" {
    # The whole point. Before this, a write landing between the commit
    # and copier's cleanliness check aborted the rebuild with
    # "Destination repository is dirty" after 26 other steps had passed.
    dir="$(_repo_with_tracked_log)"
    ic_git_exclude_logs "$dir"
    git -C "$dir" commit -qm untrack
    printf 'odoo writes again\n' >> "$dir/logs/odoo.log"
    printf 'and rotates\n' > "$dir/logs/odoo.log.2026-08-23"
    [ -z "$(git -C "$dir" status --porcelain)" ]
}

@test "ic_git_exclude_logs is idempotent" {
    dir="$(_repo_with_tracked_log)"
    ic_git_exclude_logs "$dir"
    ic_git_exclude_logs "$dir"
    ic_git_exclude_logs "$dir"
    [ "$(grep -cxF '/logs/' "$dir/.git/info/exclude")" -eq 1 ]
}

@test "ic_git_exclude_logs appends after an exclude file with no final newline" {
    dir="$(_repo_with_tracked_log)"
    printf '# no trailing newline' > "$dir/.git/info/exclude"
    ic_git_exclude_logs "$dir"
    grep -qxF '/logs/' "$dir/.git/info/exclude"
}

@test "ic_git_exclude_logs does nothing outside a git repository" {
    mkdir -p "$TMP/plain"
    run ic_git_exclude_logs "$TMP/plain"
    [ "$status" -eq 0 ]
    [ ! -e "$TMP/plain/.git" ]
}
