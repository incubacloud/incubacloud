"""Shared routing table for subprocess-backed terminal sessions.

Each active terminal session maps to a subprocess bound to a local TCP
port. Any Odoo worker can look up the port via this table and proxy HTTP
requests into the subprocess, so the session is independent of which
worker originally opened it.

The table is the **only** shared state; the subprocess itself does not
touch the DB. Liveness is checked via ``os.kill(pid, 0)`` (raises if the
process is gone) by the cleanup cron and by the controller before each
proxy call.

This is generic plumbing — a session id, a pid, a port, a bearer token
and an owner — with no notion of *what* the session connects to. Both
the instance-scoped terminal (core) and the host shell (saas) inherit
it and add nothing to the routing model itself; the capability
difference lives entirely in the session/controller, not here.
"""
import logging
import os
from datetime import timedelta

from odoo import api, fields, models

from .encrypted_char import EncryptedChar

_logger = logging.getLogger(__name__)


class CloudTerminalRouteMixin(models.AbstractModel):
    _name = 'cloud.terminal.route.mixin'
    _description = 'Terminal subprocess routing table (shared base)'
    _order = 'create_date desc'

    session_id = fields.Char(required=True, index=True)
    pid = fields.Integer(required=True)
    port = fields.Integer(required=True)
    auth_token = EncryptedChar(
        required=True,
        # Restrict ORM read to base.group_system (Odoo system admins).
        # Even cloud managers cannot pull the bearer token via Studio /
        # raw RPC / record rule bypass; the controller reads it via
        # sudo() at proxy time, which still works under groups=.
        groups='base.group_system',
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

    #: Concrete models point this at their session/audit model so the GC
    #: can reconcile sessions abandoned without an explicit /close.
    _SESSION_MODEL = None

    #: Sessions younger than this are left alone even without a route —
    #: covers the window in which a session row is committed before its
    #: route becomes visible to the GC transaction.
    _SESSION_RECONCILE_GRACE_MINUTES = 5

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

        Runs from a cron and also on every ``_resolve`` call (a cheap
        inline check), so the table stays self-healing even if the cron
        is disabled.
        """
        for route in self.sudo().search([]):
            if not route._is_process_alive():
                _logger.info(
                    "terminal route GC: unlinking stale sid=%s pid=%s",
                    route.session_id[:8], route.pid,
                )
                route.unlink()
        self._reconcile_abandoned_sessions()

    @api.model
    def _reconcile_abandoned_sessions(self):
        """Close 'open' sessions whose subprocess route no longer exists.

        A session row only reaches ``closed`` through the explicit
        ``/close`` endpoint, so a crashed browser or dropped connection
        left it open forever — and the one-console-per-target guard then
        locked its user out permanently. After the route GC above has
        removed dead routes, any open session past the grace window with
        no surviving route is abandoned: close it on the user's behalf.

        No-op on models that do not declare ``_SESSION_MODEL``.
        """
        if not self._SESSION_MODEL:
            return
        session_model = self.env[self._SESSION_MODEL].sudo()
        cutoff = fields.Datetime.now() - timedelta(
            minutes=self._SESSION_RECONCILE_GRACE_MINUTES,
        )
        stale = session_model.search([
            ('state', '=', 'open'),
            ('opened_at', '<', cutoff),
        ])
        if not stale:
            return
        alive_sids = set(
            self.sudo()
            .search([('session_id', 'in', stale.mapped('session_id'))])
            .mapped('session_id')
        )
        abandoned = stale.filtered(lambda s: s.session_id not in alive_sids)
        if not abandoned:
            return
        vals = {'state': 'closed', 'closed_at': fields.Datetime.now()}
        if 'close_reason' in session_model._fields:
            vals['close_reason'] = 'abandoned'
        abandoned.write(vals)
        _logger.info(
            "terminal session reconcile: closed %d abandoned %s row(s)",
            len(abandoned), self._SESSION_MODEL,
        )

    @api.model
    def _resolve(self, session_id):
        """Return the route for *session_id* iff the subprocess is alive.

        Auto-cleans stale rows — if the PID is dead by the time we look
        it up (subprocess crashed, idle timeout, etc.), the row is
        removed in the same transaction and ``None`` is returned so the
        caller sees the session as closed.
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
