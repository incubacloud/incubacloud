#!/usr/bin/env bash
# Print the lines of an instance's Odoo log that say where its time went.
#
# Usage: read_odoo_log.sh <instance_dir> <cutoff> <minutes>
#
#   <cutoff>   'YYYY-MM-DD HH:MM:SS' in UTC, as Odoo writes its log: lines
#              logged before it are left out.
#   <minutes>  the same window for an instance that still logs to its
#              container's output (``docker compose logs --since``).
#
# One record per line, tab-separated, the kind first:
#
#   R <stamp> <method path proto> <status> <queries> <sql s> <python s>
#                 a request, with the timing Odoo appends to it
#   C <stamp> <pid> <message>     a line of the scheduled actions (ir_cron)
#   M <stamp>                     a worker recycled for memory
#   K <stamp> <message>           a worker killed by a time limit, followed
#   + <line>                      by up to ten lines of its trace
#
# Only the projection crosses SSH: a busy instance logs tens of thousands
# of requests an hour, and the panel needs five fields of each.
#
# The panel sends this program on stdin when somebody asks for the slow
# routes, rather than uploading it as a job does, so it sources nothing.
# It reads the log as the health probe does: the live file and the newest
# archive, regular files only (``logs/`` is writable from inside the
# container, and a planted link must not be followed), the archive
# because the window straddles midnight once a day; or the container's
# output for an instance not rebuilt since file logging shipped.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: read_odoo_log.sh <instance_dir> <cutoff> <minutes>" >&2
    exit 2
fi
dir="$1"
cutoff="$2"
minutes="$3"
case "$minutes" in
    '' | *[!0-9]*) echo "not a number of minutes: $minutes" >&2; exit 2 ;;
esac
[ -d "$dir" ] || { echo "instance directory not found: $dir" >&2; exit 1; }
cd "$dir"

# Lines read back at most, as the health probe reads them.
archive_tail=5000
live_tail=20000

# Intervals are spelled out: mawk, Debian's awk, reads this.
# shellcheck disable=SC2016  # awk's own $ fields, not the shell's
program='
match($0, /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]/) {
    stamp = substr($0, RSTART, RLENGTH)
    keep = (stamp >= c)
    trace = 0
    if (!keep) next
    if (index($0, " werkzeug: ") && index($0, "\"")) {
        n = split($0, part, "\"")
        if (n >= 3) {
            split(part[n], tail, " ")
            print "R\t" stamp "\t" part[n - 1] "\t" tail[1] "\t" tail[3] "\t" tail[4] "\t" tail[5]
        }
        next
    }
    if (index($0, "ir_cron: ")) {
        message = $0
        sub(/^.*ir_cron: /, "", message)
        print "C\t" stamp "\t" $3 "\t" message
        next
    }
    if ($0 ~ /Worker \([0-9]+\) virtual memory limit \([0-9]+\) reached/) {
        print "M\t" stamp
        next
    }
    if ($0 ~ /\) timeout after [0-9]+s|CPU time limit|Dumping stacktrace of limit/) {
        # Past date, time, pid, level, database and logger.
        message = substr($0, index($0, " " $6 " ") + length($6) + 2)
        print "K\t" stamp "\t" message
        trace = 10
    }
    next
}
keep && trace > 0 { print "+\t" $0; trace-- }
'

live_is_regular() {
    [ -f logs/odoo.log ] && [ ! -L logs/odoo.log ]
}

newest_archive() {
    find logs -maxdepth 1 -type f -regextype posix-extended \
        -regex 'logs/odoo\.log\.[0-9]{4}-[0-9]{2}-[0-9]{2}(\.gz)?' \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-
}

{
    if live_is_regular; then
        archive="$(newest_archive)"
        if [ -n "$archive" ]; then
            zcat -f -- "$archive" 2>/dev/null | tail -n "$archive_tail" || true
        fi
        tail -n "$live_tail" logs/odoo.log 2>/dev/null || true
    else
        docker compose logs --no-color --no-log-prefix \
            --since "${minutes}m" odoo 2>&1 || true
    fi
} | sed -E 's/\x1b\[[0-9;]*m//g' | awk -v c="$cutoff" "$program"
