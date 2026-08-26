"""Shared routing table for subprocess-backed terminal sessions.

Each active terminal session maps to a subprocess bound to a local TCP
port. Any Odoo worker can look up the port via this table and proxy HTTP
requests into the subprocess, so the session is independent of which
worker originally opened it.

The table is the **only** shared state; the subprocess itself does not
touch the DB.

Liveness is decided two different ways on purpose. The controller, which
runs in the same container as the subprocess it spawned, asks the kernel
directly: ``os.kill(pid, 0)`` before each proxy call. The cleanup cron
cannot — since the job runner was split out, crons run in
``odoo_runner`` while terminals are spawned by ``odoo``, and a PID means
nothing across that boundary: an unrelated live process in the runner
answers "alive" for a dead terminal, and a genuinely live terminal looks
dead because its PID does not exist there. Both containers start their
processes in the same low range, so those collisions are the common case,
not the corner one. The cron therefore reaps by ``last_seen`` instead —
see :meth:`_gc`.

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

from ..terminal_session_base import SESSION_TIMEOUT
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
    last_seen = fields.Datetime(
        required=True, default=fields.Datetime.now,
        help="When the controller last confirmed the subprocess alive. "
             "The only liveness signal that crosses the container "
             "boundary, so it is what the GC cron reaps by.",
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

    #: How long a row survives without a controller confirming its
    #: subprocess. Derived from the subprocess's own idle watchdog rather
    #: than picked: it force-closes after ``SESSION_TIMEOUT`` of no
    #: keystrokes (checked on a 30 s tick), so nothing can outlive that
    #: by much. The multiplier is slack for a browser that stalls
    #: mid-session — reaping early would close a working terminal.
    _ROUTE_TTL_SECONDS = SESSION_TIMEOUT * 5

    #: Do not write ``last_seen`` more often than this. The frontend
    #: polls about once a second per open terminal; without the throttle
    #: that is one UPDATE per poll on a table every proxy call reads.
    _LAST_SEEN_THROTTLE_SECONDS = 30

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
        """Remove rows no controller has confirmed alive recently.

        Runs from a cron, in the job-runner container. It deliberately
        does **not** call :meth:`_is_process_alive`: the PIDs in this
        table belong to another container's namespace, so that answer
        would be noise (see the module docstring).

        ``last_seen`` is the honest signal. It is refreshed by
        :meth:`_resolve`, which the frontend drives roughly once a
        second while a terminal is open, and the subprocess kills itself
        after ``SESSION_TIMEOUT`` of idleness — so a row nobody has
        resolved for ``_ROUTE_TTL_SECONDS`` cannot still have a process
        behind it. The margin over the subprocess's own lifetime is
        wide on purpose: reaping a live terminal would drop the user's
        session, while reaping late only delays a row's removal.
        """
        cutoff = fields.Datetime.now() - timedelta(
            seconds=self._ROUTE_TTL_SECONDS,
        )
        stale = self.sudo().search([('last_seen', '<', cutoff)])
        for route in stale:
            _logger.info(
                "terminal route GC: unlinking sid=%s last seen %s",
                route.session_id[:8], route.last_seen,
            )
        stale.unlink()
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

        This runs on an HTTP worker, in the same container that spawned
        the subprocess, so the PID check here is meaningful — and it is
        the only place that is true. Confirming liveness also refreshes
        ``last_seen``, which is what lets the GC cron in the other
        container tell a working terminal from an abandoned row.
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
        route._touch_last_seen()
        return route

    def _touch_last_seen(self):
        """Record that this route's subprocess was just confirmed alive.

        Throttled to ``_LAST_SEEN_THROTTLE_SECONDS`` so a once-a-second
        poll does not turn into once-a-second write traffic.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        stale_after = now - timedelta(
            seconds=self._LAST_SEEN_THROTTLE_SECONDS,
        )
        if self.last_seen and self.last_seen > stale_after:
            return
        self.sudo().write({'last_seen': now})
