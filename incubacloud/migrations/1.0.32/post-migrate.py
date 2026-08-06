"""Post-migrate for 1.0.32 — re-file stale known_hosts labels.

``cloud.host.known_hosts_key`` holds the trusted server key as a
``known_hosts`` line, and that line's host pattern has to follow the
endpoint: OpenSSH files a non-default port under ``[ip]:port`` and reads
the bare ``ip`` form as port 22 only.

Hosts hardened before this release kept the label captured while they
still listened on port 22, because hardening rotates the SSH port
through the invalidation opt-out (same machine, so revoking the key
would be wrong). asyncssh accepts the bare form on any port, so every
SSH executor and the terminal kept working and the mismatch stayed
invisible — until the Ansible-backed jobs, which shell out to OpenSSH,
began reporting those hosts as unknown ("No ED25519 host key is known
for [ip]:port") and failing on host-key verification.

Relabel rather than revoke: the key material is verified and unchanged,
while revoking would stop every job on the host until an operator re-ran
TOFU by hand. Relabelling cannot widen trust either — a different
machine answering at that endpoint still fails the key check.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    hosts = env["cloud.host"].sudo().with_context(active_test=False).search(
        [("known_hosts_key", "!=", False)],
    )
    for host in hosts:
        before = (host.known_hosts_key or "").strip()
        try:
            host._relabel_known_hosts_entry()
        except Exception:
            # One unparsable entry must not block the upgrade; that host
            # keeps its stored line and can be re-trusted from the panel.
            _logger.warning(
                "1.0.32: could not re-file the SSH key label of host %s",
                host.id, exc_info=True,
            )
            continue
        if (host.known_hosts_key or "").strip() != before:
            _logger.info(
                "1.0.32: re-filed the trusted SSH key of host %s under %s",
                host.id,
                host._known_hosts_prefix(host.ip_address, host.port),
            )
