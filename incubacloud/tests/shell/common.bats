#!/usr/bin/env bats
# Tests for scripts/lib/common.sh.
#
# Run locally with:  bats incubacloud/tests/shell
# CI runs the same command in the "Shell tests (bats)" job.

setup() {
    # shellcheck source=../../scripts/lib/common.sh
    source "${BATS_TEST_DIRNAME}/../../scripts/lib/common.sh"
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
