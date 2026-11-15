"""The security events that raised or refreshed an alert, kept a week.

The security events' cron (``cloud_security_events.py``) reads each
window again from 15 minutes before where the last pass stopped, so a
line arriving late is not missed. Without a memory, an event read twice
would count twice, and one whose alert a user dismissed would raise it
again on the next pass (plan #84, point 2). This table is that memory:
one row per event that raised or refreshed an alert, nothing for the
events that did not, and never the line itself.
"""
import hashlib
from datetime import timedelta

from odoo import api, fields, models

#: Days an event is remembered: far past the cron's widest window (24 h).
EVENT_RETENTION_DAYS = 7


def event_identity(host_id, ns, line=None, audit_event=None):
    """Return what tells one security event from every other.

    The server and the auditd stamp for a change (``audit(…)``, the same
    for its records read at two Loki instants); the server, Loki's
    instant and a digest of the line for anything else. The digest keeps
    the line itself out of the database.

    :param int host_id: the ``cloud.host`` id.
    :param int ns: Loki's instant, nanoseconds.
    :param str line: the line, for an event that is one line.
    :param str audit_event: ``audit(<seconds>:<serial>)`` of a change.
    :rtype: str
    """
    if audit_event:
        return f"{host_id}:{audit_event}"
    digest = hashlib.sha256((line or "").encode("utf-8")).hexdigest()[:32]
    return f"{host_id}:{int(ns)}:{digest}"


class CloudSecurityEvent(models.Model):
    _name = "cloud.security.event"
    _description = "Security event that raised an alert"
    _order = "event_at desc, id desc"

    host_id = fields.Many2one(
        "cloud.host", required=True, ondelete="cascade", index=True,
    )
    identity = fields.Char(
        required=True, readonly=True,
        help="The server and the line's instant and digest, or the server "
             "and auditd's stamp: never the line.",
    )
    code = fields.Char(required=True, readonly=True)
    event_at = fields.Datetime(readonly=True)

    _identity_uniq = models.Constraint(
        "unique (identity)", "A security event is remembered once.",
    )

    @api.model
    def _known(self, identities):
        """Return which of *identities* are already remembered.

        :param identities: what :func:`event_identity` gave.
        :rtype: set[str]
        """
        identities = list(set(identities))
        if not identities:
            return set()
        return set(self.sudo().search(
            [("identity", "in", identities)],
        ).mapped("identity"))

    @api.model
    def _remember(self, host, identity, code, event_at):
        """Remember one event that raised or refreshed *code* on *host*.

        :param host: the ``cloud.host``.
        :param str identity: what :func:`event_identity` gave.
        :param str code: the alert's code.
        :param datetime event_at: when it happened.
        :return: False when another pass remembered it first.
        :rtype: bool
        """
        if self._known([identity]):
            return False
        self.sudo().create({
            "host_id": host.id,
            "identity": identity,
            "code": code,
            "event_at": event_at,
        })
        return True

    @api.autovacuum
    def _gc_old_events(self):
        """Forget the events older than ``EVENT_RETENTION_DAYS``.

        Run by the daily vacuum. A week is far past the widest window the
        cron reads again (24 h after an outage of the central).

        :returns: the number of rows deleted
        :rtype: int
        """
        cutoff = fields.Datetime.now() - timedelta(days=EVENT_RETENTION_DAYS)
        old = self.sudo().search([("create_date", "<", cutoff)])
        count = len(old)
        old.unlink()
        return count
