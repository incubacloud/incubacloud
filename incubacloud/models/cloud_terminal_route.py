"""Routing table for subprocess-backed terminal sessions.

Each active terminal session maps to a subprocess bound to a local
TCP port. Any Odoo worker can look up the port via this table and
proxy HTTP requests into the subprocess, so the session is
independent of which worker originally opened it.

The table is the **only** shared state; the subprocess itself
does not touch the DB. Liveness is checked via ``os.kill(pid, 0)``
(raises if the process is gone) by the cleanup cron and by the
controller before each proxy call.
"""
import logging
import os

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class CloudTerminalRoute(models.Model):
    _name = 'cloud.terminal.route'
    _description = 'Terminal subprocess routing table'
    _order = 'create_date desc'

    session_id = fields.Char(required=True, index=True)
    pid = fields.Integer(required=True)
    port = fields.Integer(required=True)
    auth_token = fields.Char(
        required=True,
        help="Shared secret the controller sends as Bearer token on "
             "every proxied request. Different per session so one "
             "compromised session can't reach others.",
    )
    user_id = fields.Many2one(
        'res.users', required=True, ondelete='cascade',
        help="Ownership for quick ACL check on every proxy call.",
    )

    _session_id_uniq = models.Constraint(
        'unique (session_id)',
        'One row per terminal session.',
    )

    # ── Liveness ────────────────────────────────────────────────────────

    def _is_process_alive(self):
        """Return True if the subprocess PID still exists on this host.

        ``os.kill(pid, 0)`` sends no signal; it only tests for the
        process's existence. OSError means the PID is gone (or the
        caller has no permission, which would mean the subprocess
        lives under a different UID — not our case).
        """
        self.ensure_one()
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    # ── GC ──────────────────────────────────────────────────────────────

    @api.model
    def _gc(self):
        """Remove rows whose subprocess is no longer running.

        Runs daily from a cron and also on every ``_resolve`` call (a
        cheap inline check), so the table stays self-healing even if
        the cron is disabled.
        """
        for route in self.sudo().search([]):
            if not route._is_process_alive():
                _logger.info(
                    "terminal route GC: unlinking stale sid=%s pid=%s",
                    route.session_id[:8], route.pid,
                )
                route.unlink()

    @api.model
    def _resolve(self, session_id):
        """Return the route for *session_id* iff the subprocess is alive.

        Auto-cleans stale rows — if the PID is dead by the time we
        look it up (subprocess crashed, idle timeout, etc.), the row
        is removed in the same transaction and ``None`` is returned
        so the caller sees the session as closed.
        """
        route = self.sudo().search(
            [('session_id', '=', session_id)], limit=1,
        )
        if not route:
            return None
        if not route._is_process_alive():
            _logger.info(
                "terminal route stale: sid=%s pid=%s",
                session_id[:8], route.pid,
            )
            route.unlink()
            return None
        return route
