#!/bin/sh
# Put the SSH configuration back as the hardening found it.
#
# Usage: ssh_put_back.sh KEEP_DIR
#
# KEEP_DIR is where the hardening kept, before it changed anything, its
# own sshd drop-in under both its names, the ssh.socket drop-in directory,
# and a ``units`` file with one "<unit> <is-enabled> <is-active>" line for
# ssh.socket and for ssh.service. The kept files go back, the ones the run
# added go, and each unit is enabled and started as it was: the service
# is stopped before the socket restarts, since systemd does not start a
# socket while its service runs.
#
# Run by the play's rescue, and by the timer the play arms on the host,
# which does the same when the play's own connection is lost on the way:
# Ansible runs no rescue for a host it cannot reach. The ``put-back-ran``
# mark it leaves tells the play that sshd may be on its old port again.
# Without a ``units`` file nothing was changed, and nothing is done.
#
# IC_SSH_PUT_BACK_ROOT is put in front of /etc, for the tests.
set -u

keep=${1:?usage: ssh_put_back.sh KEEP_DIR}
root=${IC_SSH_PUT_BACK_ROOT:-}
sshd_d=$root/etc/ssh/sshd_config.d
system_d=$root/etc/systemd/system

if [ ! -f "$keep/units" ]; then
    echo "Nothing to put back: the SSH configuration was not changed."
    exit 0
fi
: > "$keep/put-back-ran"

rm -f "$sshd_d/00-incubacloud-hardening.conf" \
      "$sshd_d/99-incubacloud-hardening.conf"
for f in "$keep"/*-incubacloud-hardening.conf; do
    if [ -e "$f" ]; then cp -a "$f" "$sshd_d/"; fi
done
rm -rf "$system_d/ssh.socket.d"
if [ -d "$keep/ssh.socket.d" ]; then
    cp -a "$keep/ssh.socket.d" "$system_d/"
fi
systemctl daemon-reload

# The kept state of a unit: field 2 is is-enabled, field 3 is-active.
state() { awk -v u="$1" -v f="$2" '$1 == u { print $f }' "$keep/units"; }

for unit in ssh.socket ssh.service; do
    case "$(state "$unit" 2)" in
        enabled) systemctl enable "$unit" ;;
        disabled) systemctl disable "$unit" ;;
    esac
done
if [ "$(state ssh.socket 3)" = active ]; then
    systemctl stop ssh.service
    systemctl restart ssh.socket
else
    systemctl stop ssh.socket 2>/dev/null
    systemctl restart ssh.service
fi
echo "SSH configuration put back: $(tr '\n' ';' < "$keep/units")"
