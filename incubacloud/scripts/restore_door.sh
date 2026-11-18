#!/usr/bin/env bash
# What an upload door's key runs (``cloud.restore.upload.grant``).
#
# Usage:
#   restore_door.sh install <path>
#   <path> receive <wrapper> <wrapper's options...> <door's directory>
#
# ``install`` copies this script to <path>, mode 0500, in one rename: the
# job that opens a door puts it in the doors' root, which only the account
# the panel connects as can reach, and a door already open keeps reading
# the copy it started with.
#
# ``receive`` is the key's forced command, run under the key's file size
# limit (``ulimit -f``, ``forced_command``). A door takes one file:
# ``restore.zip``, written in place. Only the command a client sends for
#
#   rsync -P --inplace <archive> <account>@<server>:restore.zip
#
# goes on to <wrapper> (``rrsync``, which checks it again, confines it to
# the door's directory, write only): ``rsync --server``, options that
# send one file and write nothing beside it, ``--inplace`` (or
# ``--append``) among them, then ``. restore.zip``. And ``restore.zip``
# is created empty first, when nothing is at that path: rsync then refuses
# to send more than one file to it ("destination must be a directory").
# So the key's file size limit bounds all that the door holds, not each
# file of it, and the doors of a server, each held to its own share of the
# free disk, cannot fill it together (18-nov-2026). Anything else is
# refused before rsync starts, with what to send instead.
#
# Refused: recursion and directories (``-r``, ``-d``, ``-a``), which
# replace ``restore.zip`` with a directory of files; ``-R``; backups
# (``-b``), which keep a second copy; ``-s``, whose names travel past this
# check; a temporary file beside it (no ``--inplace``); and every option
# not named below. Of the short options, only those ``rsync -P --inplace``
# sends with ``-v``, ``-t`` and ``-z`` added: ``-X`` and ``-A`` would
# store metadata outside ``ulimit -f`` and ``du``, ``-D`` a device or a
# special file, ``-o``, ``-g`` and ``-p`` an owner, a group and a mode that
# hold where the panel connects as root (18-nov-2026). Bash alone;
# nothing to source.
#
# Exit codes:
#   0   what <wrapper> exits with, or ``install`` done
#   1   refused: nothing was received
set -euo pipefail

# The short options of ``rsync --server`` a one-file upload may carry,
# before the protocol's ``e.<capabilities>``: what ``rsync -P --inplace``
# sends with ``-v``, ``-t`` and ``-z`` (rsync 3.2.7 sends ``-vtze.…``).
readonly SHORT_OK='vtz'

# refuse <why>: say why the upload is refused, and stop.
refuse() {
    printf '%s\n' "This upload door takes one file, sent as restore.zip with rsync --inplace: $1. Nothing was received. Send it with: rsync -P --inplace -e \"ssh -i <key>\" <your-backup.zip> <account>@<server>:restore.zip" >&2
    exit 1
}

case "${1:-}" in
    install)
        [ $# -eq 2 ] || { echo "usage: restore_door.sh install <path>" >&2; exit 2; }
        target="$2"
        # A name of its own beside the target, removed if the copy fails:
        # one left by an earlier install, read only, never stops this one.
        tmp="$(mktemp -- "$target.new.XXXXXX")"
        trap 'rm -f -- "$tmp"' EXIT
        cp -- "$0" "$tmp"
        chmod 500 "$tmp"
        mv -fT -- "$tmp" "$target"
        exit 0
        ;;
    receive)
        shift
        [ $# -ge 2 ] || { echo "usage: <door> receive <wrapper> ... <dir>" >&2; exit 2; }
        ;;
    *)
        echo "usage: restore_door.sh install <path> | receive <wrapper> ... <dir>" >&2
        exit 2
        ;;
esac

sent="${SSH_ORIGINAL_COMMAND:-}"
# Words split as the wrapper splits them: on single spaces, nothing a
# shell or rsync would read otherwise.
case "$sent" in
    *[!A-Za-z0-9\ .=%,_:+-]* | *"  "* | " "* | *" ") refuse "an unexpected command" ;;
esac
set -f
# shellcheck disable=SC2086  # split on purpose, no globbing (set -f)
set -- "$@" -- $sent
# "$@" is now: <wrapper> ... <dir> -- rsync --server <options> . restore.zip
wrapper=()
while [ "$1" != -- ]; do
    wrapper+=("$1")
    shift
done
shift
directory="${wrapper[${#wrapper[@]}-1]}"

if [ $# -lt 4 ] || [ "$1" != rsync ] || [ "$2" != --server ]; then
    refuse "not an rsync upload"
fi
shift 2
if [ "${*: -2:1}" != . ] || [ "${*: -1}" != restore.zip ]; then
    refuse "its destination must be restore.zip"
fi
inplace=
while [ $# -gt 2 ]; do
    option="$1"
    shift
    case "$option" in
        --inplace | --append) inplace=1 ;;
        --log-format=* | --bwlimit=* | --timeout=* | --compress-level=* \
            | --compress-choice=* | --checksum-choice=* | --max-size=* \
            | --min-size=* | --size-only) ;;
        --*) refuse "the option ${option%%=*} is not taken" ;;
        -*)
            letters="${option#-}"
            letters="${letters%%e*}"
            case "$letters" in
                *[!"$SHORT_OK"]*) refuse "the option -${letters//[$SHORT_OK]/} is not taken (only -v, -t and -z, beside -P and --inplace)" ;;
            esac
            ;;
        *) refuse "it names more than restore.zip" ;;
    esac
done
[ -n "$inplace" ] || refuse "without --inplace, rsync writes a second file beside it"

cd -- "$directory"
# A file already there stays; a link or anything else at that path is left
# for rsync, which writes no second file next to it either.
if [ ! -e restore.zip ] && [ ! -L restore.zip ]; then
    (set -C; : > restore.zip) 2>/dev/null || true
fi
exec "${wrapper[@]}"
