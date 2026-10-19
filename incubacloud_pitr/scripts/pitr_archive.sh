#!/bin/sh
# The database's ``archive_command`` for continuous recovery:
#   archive_command = '/opt/walg/archive %f'
#
# The database container holds no credentials: Odoo connects as a
# superuser, and a superuser's SQL can read that container's environment.
# The ``pitr`` agent, which has them, uploads every segment PostgreSQL
# marks ready and then leaves a mark named after it in the spool. This
# waits for that mark, up to a minute, and fails otherwise: PostgreSQL
# keeps the segment, retries, and counts the failure in pg_stat_archiver,
# which is what the panel watches.
#
# Installed into the instance's .walg/ by ``pitr.sh install``.
# IC_SPOOL and IC_ARCHIVE_WAIT override the spool and the wait (tests).
name="$1"
spool="${IC_SPOOL:-/opt/walg/spool}"
wait="${IC_ARCHIVE_WAIT:-60}"
i=0
while [ "$i" -lt "$wait" ]; do
    if [ -e "$spool/done/$name" ]; then
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done
echo "continuous recovery: $name was not uploaded within $wait s (is the pitr agent running?)" >&2
exit 1
