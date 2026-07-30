#!/usr/bin/env bash
# Shared helpers sourced by every incubacloud remote operation script.
#
# Entry scripts live next to this file under ``scripts/`` and are
# uploaded to the host by ``AbstractExecutor.run_script()``, which
# invokes them as ``bash <dir>/<name>.sh <args...>``. Source this
# library with:
#
#     source "$(dirname "$0")/lib/common.sh"
#
# It deliberately does NOT enable strict mode on behalf of its caller:
# every entry script declares its own ``set -euo pipefail`` so that
# choice stays visible where the script is read.
#
# The ERROR prefix is not cosmetic: the job log classifier renders any
# line matching \b(ERROR|FATAL|CRITICAL)\b as a red [err] entry, so
# ic_die surfaces correctly in the panel without extra plumbing.

# Print a progress line to the job log.
ic_log() {
    printf '[incubacloud] %s\n' "$*"
}

# Print a non-fatal warning to the job log.
ic_warn() {
    printf '[incubacloud] WARNING: %s\n' "$*" >&2
}

# Print an error and abort the script with a non-zero exit status.
ic_die() {
    printf '[incubacloud] ERROR: %s\n' "$*" >&2
    exit 1
}

# Expand a leading ``~/`` in a path.
#
# Instance directories are stored as ``~/project/instance`` (see
# cloud.instance.get_remote_dir) and reach a script as a quoted
# argument, so the shell never expands the tilde for us. Expanding it
# here — on the host, against the real $HOME — is also more correct than
# guessing the remote home from the panel.
ic_expand_home() {
    # SC2088 fires on the literal tilde in these patterns, which is
    # precisely the point: we are matching an *unexpanded* tilde that
    # arrived as data, not writing one we expect the shell to expand.
    # shellcheck disable=SC2088
    case "$1" in
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s\n' "${HOME}${1#\~}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# Abort unless a script received the arguments it needs.
# Usage: ic_require_args <needed> "$#" "<usage line>"
ic_require_args() {
    local needed="$1" given="$2" usage="${3-}"
    if [ "$given" -lt "$needed" ]; then
        ic_die "expected at least ${needed} argument(s), got ${given}.${usage:+ Usage: ${usage}}"
    fi
}
