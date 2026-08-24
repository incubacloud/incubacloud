#!/usr/bin/env bash
# Prepare (or tear down) the host side of an instance's Odoo log archive.
#
# Usage:
#   instance_logs.sh install <instance_dir> <name> <days>
#   instance_logs.sh remove  <name>
#
# ``install`` creates ``<instance_dir>/logs`` owned by the container's
# uid and writes ``/etc/logrotate.d/incubacloud-<name>`` so the host's
# daily logrotate run turns ``odoo.log`` into ``odoo.log.<date>(.gz)``.
# ``remove`` drops that config when the instance is deleted.
#
# Both are idempotent: install doubles as a repair and runs on every
# deploy and rebuild.
#
# Only a missing instance directory, a name that is not filename-safe
# and a bad retention are fatal. Everything else warns: a host where
# the operator cannot escalate still gets its logs — Odoo falls back to
# the container's output, whose size the compose override caps, and the
# health probe raises ``instance_logs_unhealthy`` so the degradation is
# visible instead of silent.
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# The uid doodba's images run Odoo as. The container writes the log
# directly, so the directory has to belong to it.
ODOO_UID=1000
ODOO_GID=1000

# Overridable so the bats suite can exercise the real logic without
# writing to the host's /etc.
LOGROTATE_DIR="${IC_LOGROTATE_DIR:-/etc/logrotate.d}"

# Run a command as root, without assuming sudo exists: hosts reached as
# root (the common case for an unhardened one) may not ship it.
ic_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

ic_require_args 2 "$#" \
    "instance_logs.sh install <instance_dir> <name> <days> | remove <name>"

op="$1"

# The name becomes a file name under /etc/logrotate.d and is otherwise
# panel-supplied; refuse anything a shell or a path could reinterpret.
check_name() {
    case "$1" in
        *[!A-Za-z0-9_.-]*|""|.|..)
            ic_die "unsafe instance name: $1" ;;
    esac
}

case "$op" in
    install)
        ic_require_args 4 "$#" \
            "instance_logs.sh install <instance_dir> <name> <days>"
        dir="$(ic_expand_home "$2")"
        name="$3"
        days="$4"
        check_name "$name"
        case "$days" in
            ''|*[!0-9]*) ic_die "retention must be a positive integer: $days" ;;
        esac
        [ "$days" -ge 1 ] || ic_die "retention must be at least 1 day: $days"
        [ -d "$dir" ] || ic_die "instance directory not found: $dir"

        logs="$dir/logs"
        mkdir -p "$logs" || ic_die "could not create $logs"
        # Docker would create the mount source itself, as root, leaving
        # the container unable to write; owning it up front is what
        # keeps Odoo on the file instead of falling back to stdout.
        owner="$(stat -c %u "$logs" 2>/dev/null || echo -1)"
        if [ "$owner" != "$ODOO_UID" ]; then
            ic_root chown "$ODOO_UID:$ODOO_GID" "$logs" \
                || ic_warn "could not chown $logs to $ODOO_UID: Odoo may fall back to container output"
        fi
        chmod 0755 "$logs" 2>/dev/null || true
        # This directory sits inside the copier repo, so a tracked log
        # would make every later rebuild race with Odoo's own writes.
        ic_git_exclude_logs "$dir"

        if ! command -v logrotate >/dev/null 2>&1; then
            ic_log "Installing logrotate..."
            ic_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq logrotate \
                || ic_warn "could not install logrotate: the log will not be archived"
        fi

        conf="$LOGROTATE_DIR/incubacloud-$name"
        tmp="$(mktemp)"
        cat > "$tmp" <<EOF
# Managed by IncubaCloud — rewritten on every deploy and rebuild.
# Odoo writes this file through a WatchedFileHandler, so it reopens the
# log by itself after the rename: no signal, no restart, no lost lines.
$logs/odoo.log {
    daily
    rotate $days
    missingok
    notifempty
    compress
    delaycompress
    dateext
    dateformat .%Y-%m-%d
    dateyesterday
    nocreate
}
EOF
        # ``nocreate`` on purpose: a file logrotate created would belong
        # to root and the container (uid $ODOO_UID) could not write to
        # it. Odoo recreates it on its next line, owned by itself.
        if ic_root mkdir -p "$LOGROTATE_DIR" && ic_root cp "$tmp" "$conf"; then
            ic_root chmod 0644 "$conf" || true
            ic_log "logrotate config written to $conf (rotate $days)"
            if command -v logrotate >/dev/null 2>&1; then
                ic_root logrotate -d "$conf" >/dev/null 2>&1 \
                    || ic_warn "logrotate rejected $conf; the log will not be archived"
            fi
        else
            ic_warn "could not write $conf: the log will grow without archiving"
        fi
        rm -f "$tmp"

        # A config nothing ever runs is the silent failure this feature
        # is about, so say so where the operator reads job logs.
        if ! systemctl is-enabled logrotate.timer >/dev/null 2>&1 \
            && [ ! -f /etc/cron.daily/logrotate ]; then
            ic_warn "no logrotate timer or cron.daily entry found: nothing will rotate the log"
        fi
        ic_log "log directory ready at $logs"
        ;;
    remove)
        name="$2"
        check_name "$name"
        conf="$LOGROTATE_DIR/incubacloud-$name"
        if [ -e "$conf" ]; then
            ic_root rm -f "$conf" || ic_warn "could not remove $conf"
            ic_log "removed $conf"
        else
            ic_log "no logrotate config for $name; nothing to remove."
        fi
        ;;
    *)
        ic_die "unknown operation: $op (expected install or remove)"
        ;;
esac
