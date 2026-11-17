"""What a server's Access tab shows: how the panel signs in, and who did.

The server is its owner's, and the panel is the owner's tool on it. The
tab says, from what the panel already holds:

- the account the panel connects as, the address and port, and the
  fingerprint of the key it signs in with (the one the server's
  ``authorized_keys`` lists), beside the server's own host key;
- the last sign-ins, from the security logs the server sends
  (``_security_board``'s ``accepted`` table, seven days), each told as the
  panel's or someone else's;
- what deleting the server from the panel takes off it, which the page
  states from the teardown (``ansible/playbooks/host_teardown.yml``).

Read by whoever manages servers, as the security tables are. A read that
fails reaches the browser as ``error`` alone, which the tab words itself;
what went wrong goes to the server's log.
"""
import logging

from odoo import models

_logger = logging.getLogger(__name__)

#: The sign-ins the tab lists; the Security view has the rest.
ACCESS_SIGN_INS = 10


class CloudHost(models.Model):
    """The Access tab's read."""

    _inherit = "cloud.host"

    def _access_summary(self):
        """Return what the Access tab shows of this server.

        :return: ``user``, ``address``, ``port``, ``login_type``,
            ``panel_key_fingerprint`` (``''`` without a key it can read),
            ``host_key_fingerprint``, ``hardened`` and ``sign_ins``: its
            ``state`` (``ready``, ``off``, ``unavailable`` or ``error``),
            and when ready its ``rows`` (newest first, at most
            :data:`ACCESS_SIGN_INS`) and ``more`` (whether there are others)
        :rtype: dict
        """
        self.ensure_one()
        fingerprints = self.sudo()._panel_key_fingerprints()
        return {
            "user": self.user or "",
            "address": self.ip_address or "",
            "port": self.port,
            "login_type": self.login_type,
            "panel_key_fingerprint": fingerprints[0] if fingerprints else "",
            "host_key_fingerprint": self.known_hosts_fingerprint or "",
            "hardened": bool(self.hardened),
            "sign_ins": self._access_sign_ins(),
        }

    def _access_sign_ins(self):
        """Return the server's last sign-ins, as the Security view tells them.

        A failed read is said as ``{"state": "error"}`` and nothing more:
        the central's or the query's message may name addresses, tokens or
        internals the browser has no use for, so it goes to the log.

        :rtype: dict
        """
        self.ensure_one()
        try:
            board = self._security_board(range_key="7d", detail=True)
        except Exception:  # noqa: BLE001 — the tab still shows the rest
            _logger.warning(
                "Access tab of host %s: the sign-ins could not be read",
                self.id, exc_info=True,
            )
            return {"state": "error"}
        if not board.get("ok"):
            _logger.warning(
                "Access tab of host %s: the security board answered %s",
                self.id, board.get("error") or board.get("reason") or "no error",
            )
            return {"state": "error"}
        if board.get("state") != "ready":
            return {"state": board.get("state") or "unavailable"}
        accepted = (board.get("tables") or {}).get("accepted") or {}
        if accepted.get("error"):
            _logger.warning(
                "Access tab of host %s: the sign-ins table answered %s",
                self.id, accepted["error"],
            )
            return {"state": "error"}
        rows = accepted.get("rows") or []
        return {
            "state": "ready",
            "rows": rows[:ACCESS_SIGN_INS],
            "more": len(rows) > ACCESS_SIGN_INS or bool(accepted.get("cut")),
        }
