#!/usr/bin/env bash
# Prepare the host side of the proxy's access log.
#
# Usage:
#   proxy_access_log.sh install
#
# Creates the directory the proxy writes ``access.log`` into (mounted
# into the container at the same path) and writes
# ``/etc/logrotate.d/incubacloud-traefik``: rotated daily, compressed,
# kept 30 days. A file over 100 MB is rotated on the next daily run
# without waiting for the day to end, and a busy host keeps the full
# 30 days because the count is bounded by age, not by number of files.
# Traefik is sent USR1 after each rotation, which makes it reopen the
# file, so no line is lost.
#
# The size limit only counts when logrotate runs, and the distro runs
# it once a day: a host flooded with requests writes gigabytes between
# two runs and fills its disk before the limit is ever looked at. So a
# timer of our own runs this one entry every ten minutes. With ``daily``
# in the entry, a run rotates only when the day has changed or the file
# is over the limit; the other runs do nothing.
#
# Idempotent: runs on every Full Setup. Nothing here is fatal: a host
# where the operator cannot escalate still serves traffic; the proxy
# then creates the directory through its bind mount and the log grows
# without rotation, which is said in the job log.
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# Overridable so the bats suite can exercise the real logic without
# writing to the host's /etc and /var/log.
LOGROTATE_DIR="${IC_LOGROTATE_DIR:-/etc/logrotate.d}"
LOG_DIR="${IC_PROXY_LOG_DIR:-/var/log/traefik}"
SYSTEMD_DIR="${IC_SYSTEMD_DIR:-/etc/systemd/system}"
# The container compose names the proxy service; USR1 is how Traefik
# is told to reopen its log files.
PROXY_CONTAINER="${IC_PROXY_CONTAINER:-inverseproxy-proxy-1}"
TIMER_UNIT="incubacloud-traefik-logrotate"

ic_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

ic_require_args 1 "$#" "proxy_access_log.sh install"

case "$1" in
    install)
        ic_root mkdir -p "$LOG_DIR" \
            || ic_warn "could not create $LOG_DIR: the proxy will create it through its mount"
        ic_root chmod 0755 "$LOG_DIR" 2>/dev/null || true

        if ! command -v logrotate >/dev/null 2>&1; then
            ic_log "Installing logrotate..."
            ic_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq logrotate \
                || ic_warn "could not install logrotate: the access log will not be rotated"
        fi

        conf="$LOGROTATE_DIR/incubacloud-traefik"
        tmp="$(mktemp)"
        cat > "$tmp" <<CONF
# Managed by IncubaCloud — rewritten on every Full Setup.
# The proxy's access log: one JSON line per request. 30 days, by age:
# a busy host rotating on size more than once a day still keeps them.
$LOG_DIR/access.log {
    daily
    maxsize 100M
    maxage 30
    rotate 400
    missingok
    notifempty
    compress
    delaycompress
    dateext
    dateformat .%Y-%m-%d-%s
    sharedscripts
    postrotate
        docker kill --signal=USR1 $PROXY_CONTAINER >/dev/null 2>&1 || true
    endscript
}
CONF
        if ic_root mkdir -p "$LOGROTATE_DIR" && ic_root cp "$tmp" "$conf"; then
            ic_root chmod 0644 "$conf" || true
            ic_log "logrotate config written to $conf"
            if command -v logrotate >/dev/null 2>&1; then
                ic_root logrotate -d "$conf" >/dev/null 2>&1 \
                    || ic_warn "logrotate rejected $conf; the access log will not be rotated"
            fi
        else
            ic_warn "could not write $conf: the access log will grow without rotation"
        fi
        rm -f "$tmp"

        # The ten-minute timer. It shares logrotate's state file with the
        # distro's daily run on purpose: that is how each knows the other
        # rotated today, and logrotate locks it, so two runs meeting at
        # midnight cannot rotate twice.
        logrotate_bin="$(command -v logrotate || echo /usr/sbin/logrotate)"
        service_tmp="$(mktemp)"
        timer_tmp="$(mktemp)"
        cat > "$service_tmp" <<UNIT
# Managed by IncubaCloud — rewritten on every Full Setup.
[Unit]
Description=Rotate the proxy access log when it is over its size limit

[Service]
Type=oneshot
ExecStart=$logrotate_bin $conf
UNIT
        cat > "$timer_tmp" <<UNIT
# Managed by IncubaCloud — rewritten on every Full Setup.
# The daily system run looks at the size limit once a day; a flooded
# host fills its disk long before that.
[Unit]
Description=Check the proxy access log size every ten minutes

[Timer]
OnCalendar=*:0/10
AccuracySec=1min

[Install]
WantedBy=timers.target
UNIT
        if ic_root mkdir -p "$SYSTEMD_DIR" \
            && ic_root cp "$service_tmp" "$SYSTEMD_DIR/$TIMER_UNIT.service" \
            && ic_root cp "$timer_tmp" "$SYSTEMD_DIR/$TIMER_UNIT.timer"; then
            ic_root chmod 0644 "$SYSTEMD_DIR/$TIMER_UNIT.service" "$SYSTEMD_DIR/$TIMER_UNIT.timer" || true
            if ic_root systemctl daemon-reload \
                && ic_root systemctl enable --now "$TIMER_UNIT.timer" >/dev/null 2>&1; then
                ic_log "size check timer enabled ($TIMER_UNIT.timer, every 10 minutes)"
            else
                ic_warn "could not enable $TIMER_UNIT.timer: the size limit is only checked by the daily run"
            fi
        else
            ic_warn "could not write the $TIMER_UNIT units: the size limit is only checked by the daily run"
        fi
        rm -f "$service_tmp" "$timer_tmp"

        if ! systemctl is-enabled logrotate.timer >/dev/null 2>&1 \
            && [ ! -f /etc/cron.daily/logrotate ]; then
            ic_warn "no logrotate timer or cron.daily entry found: nothing will rotate the access log"
        fi
        ic_log "proxy access log ready at $LOG_DIR/access.log"
        ;;
    *)
        ic_die "unknown operation: $1"
        ;;
esac
