import fnmatch
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from datetime import timedelta

import psycopg2
from markupsafe import escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import config as odoo_config

from ..github.http_utils import safe_urlopen
from ..mail_layout import wrap_in_mail_layout
from ..net.outbound import post_json
from .res_users_ext import as_platform

_logger = logging.getLogger(__name__)

#: Every alert code core raises with ``audience='provider'`` (``*``
#: stands for a variable part). A provider alert leaves the panel for
#: whoever runs the service behind it, instead of paging the operator who
#: sees it, so each one is a decision: ``test_provider_alert_inventory``
#: reads the code and fails on one missing here. Layers that raise their
#: own keep their list next to them.
PROVIDER_ALERT_CODES = frozenset({
    # A secret the key chain cannot open (``encrypted_char``).
    "encrypted_value_unreadable:*.*",
    # The metrics central, when this panel reads it but does not run it.
    "metrics_backend_unreachable",
    # A deleted instance whose record could not be removed.
    "instance_unlink_failed",
    # A prune that swept what the protect label should have spared.
    "prune_swept_managed",
    "prune_swept_network",
})


class CloudAlert(models.Model):
    _name = "cloud.alert"
    _description = "Cloud Infrastructure Alert"
    _order = "create_date desc"

    host_id = fields.Many2one(
        "cloud.host",
        ondelete="cascade",
        string="Host",
        index=True,
    )
    instance_id = fields.Many2one(
        "cloud.instance",
        ondelete="cascade",
        string="Instance",
        index=True,
    )
    project_id = fields.Many2one(
        "cloud.project",
        ondelete="cascade",
        string="Project",
        index=True,
    )
    conflict_data = fields.Json(
        string="Conflict Data",
        help="List of pip dependency conflicts: [{name, existing, incoming}]",
    )
    payload = fields.Json(
        string="Alert Payload",
        help=(
            "Generic structured payload attached to the alert. "
            "Used by instance_error_logs (deduped log line groups), "
            "and reserved for any future alert that needs to ship "
            "structured detail beyond the human-readable message."
        ),
    )
    job_id = fields.Many2one(
        "cloud.job",
        ondelete="set null",
        string="Originating Job",
        index=True,
    )
    code = fields.Char(
        required=True,
        index=True,
        help="Machine-readable identifier for deduplication (e.g. disk_critical).",
    )
    dedup_key = fields.Char(
        readonly=True,
        index=True,
        help="What tells two alerts with the same code and target apart, "
             "when the target alone does not: the backup backend, the "
             "type of the job that failed. Part of the alert's identity "
             "in raise_alert; empty for most alerts.",
    )
    message = fields.Char(required=True)
    level = fields.Selection(
        [("warning", "Warning"), ("critical", "Critical")],
        default="warning",
        required=True,
    )
    audience = fields.Selection(
        [("fleet", "Fleet operator"), ("provider", "Service provider")],
        default="fleet",
        required=True,
        help="Who can act on this alert. 'Fleet operator': whoever runs "
             "the hosts, instances and repositories of this panel. "
             "'Service provider': whoever runs a service this panel "
             "depends on but does not own, such as the metrics central "
             "it reads or the key that encrypts its secrets. On a panel "
             "that owns everything both are the same person; on a hosted "
             "one, provider alerts are taken to the provider instead of "
             "paging the operator about something they cannot touch.",
    )
    state = fields.Selection(
        [("active", "Active"), ("dismissed", "Dismissed")],
        default="active",
        required=True,
    )
    occurrences = fields.Integer(
        default=1,
        readonly=True,
        help=(
            "How many times this alert has been raised while it stayed "
            "active. Only ``job_failed`` counts today: a cron job that "
            "keeps failing every 30 minutes is one incident, not one "
            "per attempt, and stacking a row (and a notification) per "
            "attempt is how a single broken thing buries every other "
            "alert in the panel."
        ),
    )
    create_date = fields.Datetime(readonly=True)
    was_critical = fields.Boolean(
        readonly=True,
        help=(
            "Whether this alert was critical at any point while active. "
            "Who is told it is over is decided by the worst it was, not "
            "by its level at the end: an alert that paged someone as "
            "critical and stepped down before clearing would otherwise "
            "close without a word to exactly those people."
        ),
    )
    last_raised_at = fields.Datetime(
        string="Last Seen",
        readonly=True,
        help=(
            "When the condition behind this alert was last observed. "
            "Refreshed on every raise, so an alert that keeps being "
            "re-raised shows how recent the problem is rather than "
            "when it first appeared. Nullable: rows written before "
            "this field existed fall back to ``create_date``."
        ),
    )
    resolution = fields.Selection(
        [("auto", "Resolved automatically"), ("manual", "Closed by a user")],
        readonly=True,
        help=(
            "How the alert left the active list: the platform saw the "
            "problem go (a later run of the failed job succeeded, the "
            "condition cleared), or a user dismissed or resolved it. "
            "Empty on alerts closed before this was recorded."
        ),
    )
    resolved_at = fields.Datetime(
        readonly=True,
        help="When the alert left the active list.",
    )
    resolved_by_job_id = fields.Many2one(
        "cloud.job",
        string="Resolved By Job",
        readonly=True,
        ondelete="set null",
        help="The job whose success closed this failure alert.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("level") == "critical":
                vals["was_critical"] = True
        records = super().create(vals_list)
        for record in records.filtered(
            lambda r: r.state == "active",
        ):
            record._dispatch_notifications()
        return records

    def init(self):
        """Enforce the dedup rule in the database, not just in Python.

        ``raise_alert`` was a search-then-create with nothing holding
        the gap open, and Odoo cursors run at REPEATABLE READ: two
        producers that fire in the same instant each read a snapshot
        where the other's row does not exist, so both insert. That is
        not theoretical — on 2026-08-29 the host-metrics probe and the
        instance-health probe both raised ``host_unreachable`` for the
        same host 142 ms apart, and the panel showed the same incident
        twice. A lock would not have helped: under snapshot isolation
        the loser still cannot see the winner's row. Only a unique
        index can decide this, so it decides it here.

        Two carve-outs, both deliberate:

        * ``job_failed`` is keyed by *job type* as well as target (see
          ``cloud.job._job_failed_domain``), so several may legitimately
          be active on one host at once — a failing backup and a failing
          rebuild are two incidents, not one.
        * targetless rows without a key (global platform events) are left
          alone: they are rare, and have no natural dedup key.

        The DELETE is a one-shot dedupe keeping the newest row per key
        — newest because its message is the freshest description of a
        situation that is still open. It is idempotent: on later
        upgrades there is nothing left to collapse.
        """
        cr = self.env.cr
        cr.execute(
            """
            UPDATE cloud_alert a
               SET state = 'dismissed'
              FROM cloud_alert b
             WHERE a.state = 'active'
               AND b.state = 'active'
               AND a.code = b.code
               AND a.code <> 'job_failed'
               AND (a.instance_id IS NOT NULL
                    OR COALESCE(a.host_id, 0) = COALESCE(b.host_id, 0))
               AND COALESCE(a.instance_id, 0) = COALESCE(b.instance_id, 0)
               AND COALESCE(a.dedup_key, '') = COALESCE(b.dedup_key, '')
               AND (a.host_id IS NOT NULL OR a.instance_id IS NOT NULL
                    OR a.dedup_key IS NOT NULL)
               AND a.id < b.id
            """
        )
        # The identity changed in 1.0.193 (``_dedup_domain``): the key
        # joined it, and an instance's alert stopped depending on the host
        # it runs on. The old index would refuse two backends' alerts on
        # the same (empty) target.
        cr.execute("DROP INDEX IF EXISTS cloud_alert_active_target_uidx")
        cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                cloud_alert_active_identity_uidx
            ON cloud_alert (
                code,
                (CASE WHEN instance_id IS NULL THEN COALESCE(host_id, 0) ELSE 0 END),
                COALESCE(instance_id, 0),
                COALESCE(dedup_key, '')
            )
            WHERE state = 'active'
              AND code <> 'job_failed'
              AND (host_id IS NOT NULL OR instance_id IS NOT NULL
                   OR dedup_key IS NOT NULL)
            """
        )

    # ===============================
    # RAISE / RESOLVE (model-level API)
    # ===============================
    #
    # The single implementation of the dedup rule. Executors reach it
    # through ``AbstractExecutor._alert``/``_resolve_alert`` (which add
    # their own cursor so an alert survives a rolled-back job), and
    # job-less producers — the metric rule cron above all — call it
    # directly. Keeping one implementation is deliberate: the same rule
    # written twice is exactly how the two copies of a recipe drift apart.

    @staticmethod
    def _dedup_domain(code, host=None, instance=None, project=None, key=None):
        """Return the domain identifying the active alert for a target.

        Identity is (code, instance, key, active) for an alert about an
        instance, and (code, host, key, active) for one about a host; the
        project too when one is given. An alert about a host and one
        about an instance are different alerts even with the same code,
        which is what lets a per-instance rule fan out without clobbering
        the host-level one. The host given with an instance only says
        where it runs: the instance's alert stays the same alert when it
        moves. *key* tells apart what the target alone does not (two
        backup backends, two types of failed job); *project* is the
        target of an alert about a whole project.
        """
        domain = [
            ("code", "=", code),
            ("state", "=", "active"),
            ("instance_id", "=", instance.id if instance else False),
            ("dedup_key", "=", key or False),
        ]
        if not instance:
            domain.append(("host_id", "=", host.id if host else False))
        if project:
            domain.append(("project_id", "=", project.id))
        return domain

    @api.model
    def _link_target(self, link_field, link_id):
        """Turn a ``(link_field, id)`` pair into ``raise_alert`` targets.

        For producers that find what their alert hangs on by field name
        (``host_id``, ``instance_id`` or ``project_id``). An alert about a
        project carries the project as its key too, so the unique index
        that guards against twin alerts covers it.

        :param str link_field: ``host_id``, ``instance_id`` or ``project_id``
        :param int link_id: the id of that record
        :return: keyword arguments for ``raise_alert``/``resolve_alert``
        :rtype: dict
        """
        if link_field == "host_id":
            return {"host": self.env["cloud.host"].browse(link_id)}
        if link_field == "instance_id":
            return {"instance": self.env["cloud.instance"].browse(link_id)}
        if link_field == "project_id":
            return {
                "project": self.env["cloud.project"].browse(link_id),
                "key": f"project:{link_id}",
            }
        raise ValueError(f"cloud.alert cannot hang on {link_field!r}")

    @api.model
    def raise_alert(self, code, message, level="warning",
                    host=None, instance=None, job=None, payload=None,
                    confirm_after=None, confirm_window=None,
                    audience="fleet", project=None, key=None, extra=None):
        """Create or refresh the active alert for (code, target).

        Idempotent: a repeated call updates the message/level of the
        existing alert instead of creating a second one, so a cron that
        re-evaluates every few minutes does not flood the operator. The
        one refresh that notifies again is a warning turning critical.

        A *measurement* confirms itself first. The first sightings are
        kept apart as a ``cloud.alert.suspicion`` — not listed, not
        counted, not notified — and the alert only exists once the
        condition has been seen enough times in a row, each sighting
        within a window of the last. How many, and how fresh, comes from
        ``cloud.alert.policy`` by code: a producer that measures raises
        its code and nothing else, so the rule "never on the first
        sample" lives in one table rather than in a streak counter per
        producer — on 2026-10-13 a single 10 s read timeout through a
        CDN paged a customer as a critical outage. A caller that owns
        its own count (a metric rule, whose code the operator writes)
        passes ``confirm_after`` and ``confirm_window`` and the table is
        not consulted. An *event* (a job that failed, a key that
        changed) has no row and raises on the spot.

        The insert runs inside a savepoint because the search above
        cannot see a row another transaction has just written (Odoo
        cursors are REPEATABLE READ). When that happens the unique
        index from ``init`` rejects the second insert, and losing that
        race is a *success*: the alert exists, raised by the producer
        that got there first. The savepoint is what keeps the rejection
        from poisoning the caller's transaction — without it the whole
        job would roll back over an alert that was already filed.

        :param str code: machine-readable dedup identifier
        :param str message: human-readable text shown in the panel
        :param str level: ``warning`` or ``critical``
        :param host: ``cloud.host`` this alert is about, if any
        :param instance: ``cloud.instance`` this alert is about, if any
        :param job: ``cloud.job`` that produced it, if any
        :param payload: structured detail stored on ``payload``, if any
        :param int confirm_after: sightings in a row before the alert
            exists; None (the default) asks ``cloud.alert.policy``,
            which answers 1 for a code it does not list
        :param int confirm_window: seconds a sighting stays fresh, an
            older one starts the count over; None asks the policy too
        :param str audience: ``fleet`` (default) or ``provider``; see the
            field. Set on creation, kept on a refresh.
        :param project: ``cloud.project`` this alert is about, for one about
            a whole project; part of its identity
        :param str key: what tells this alert apart from others with the
            same code and target (``dedup_key``); part of its identity
        :param dict extra: other fields to write on creation and on every
            refresh (``conflict_data``, ``occurrences``)
        :returns: the active ``cloud.alert`` record, or an empty
            recordset when a concurrent producer filed it first or the
            sighting is still a suspicion
        """
        # An alert is the platform noticing something, never a user
        # action — but it is raised from wherever the failure happened,
        # which may be a public webhook or a customer's signup request.
        # Elevating here covers every producer at once, present and
        # future, instead of asking each of the callers to remember.
        self = as_platform(self)
        existing = self.search(
            self._dedup_domain(
                code, host=host, instance=instance, project=project, key=key,
            ),
            limit=1,
        )
        # Stamped on create *and* on refresh: consumers that decide how
        # stale an alert is need the last sighting, not the first.
        vals = {
            **(extra or {}),
            "message": message,
            "level": level,
            "last_raised_at": fields.Datetime.now(),
        }
        if job:
            vals["job_id"] = job.id
        if payload is not None:
            vals["payload"] = payload
        if host:
            # Where an instance runs now, on a refresh after a move.
            vals["host_id"] = host.id
        if existing:
            escalated = existing.level != "critical" and level == "critical"
            if escalated:
                vals["was_critical"] = True
            existing.write(vals)
            if escalated:
                # Notifications go out when an alert is created. One that
                # turns critical while open is news to everyone whose
                # level filtered out its warning, so it goes out again.
                existing._dispatch_notifications()
            return existing
        if confirm_after is None or confirm_window is None:
            after, window = self.env["cloud.alert.policy"]._for_code(code)
            confirm_after = after if confirm_after is None else confirm_after
            confirm_window = window if confirm_window is None else confirm_window
        if confirm_after > 1 and not self.env["cloud.alert.suspicion"]._sight(
            code, host, instance, confirm_after, confirm_window,
        ):
            return self.browse()
        try:
            with self.env.cr.savepoint():
                return self.create({
                    "code": code,
                    "host_id": host.id if host else False,
                    "instance_id": instance.id if instance else False,
                    # An instance's project rides along, for the views
                    # that list a project's alerts; only a project given
                    # on its own is part of the identity.
                    "project_id": (
                        project.id if project
                        else instance.project_id.id if instance else False
                    ) or False,
                    "dedup_key": key or False,
                    "audience": audience,
                    **vals,
                })
        except psycopg2.errors.UniqueViolation:
            _logger.info(
                "cloud.alert: %s for host=%s instance=%s was raised "
                "concurrently; keeping the first one",
                code, host.id if host else None,
                instance.id if instance else None,
            )
            return self.browse()

    @api.model
    def resolve_alert(self, code, host=None, instance=None, project=None, key=None):
        """Dismiss the active alert for (code, target), if any.

        Idempotent: resolving something that was never raised is a no-op,
        so callers do not have to track whether they alerted before. The
        target is the one ``raise_alert`` was given, *project* and *key*
        included.

        :returns: the dismissed records (empty when there was nothing)
        """
        # Same reasoning as ``raise_alert``: an automatic dismissal is
        # the platform closing its own incident, so ``write_uid`` must
        # not name whoever's request happened to clear the condition.
        self = as_platform(self)
        # A clear reading also ends a suspicion that never became an
        # alert: the next sighting starts counting from one again.
        self.env["cloud.alert.suspicion"]._forget(code, host, instance)
        active = self.search(
            self._dedup_domain(
                code, host=host, instance=instance, project=project, key=key,
            ),
        )
        if active:
            active.write({"state": "dismissed", "resolution": "auto"})
            # On-call channels saw the problem; they must also see the
            # closure, or every incident looks permanently open from
            # Telegram/webhook. Only *automatic* resolution notifies:
            # an operator dismissing by hand in the panel is already
            # looking at it and chose to silence it.
            for record in active:
                record._notify_alert_resolved()
        return active

    @api.model
    def resolve_instance_alerts(self, instance):
        """Dismiss every active alert about *instance*, whatever its code.

        For an instance leaving the daily operation (archived): nothing
        probes it any more, so an alert still lit about it — down, error
        logs, a backup that failed — would stay lit for ever. Dismissed
        as the platform and announced as resolved, like
        :meth:`resolve_alert`.

        :param instance: the ``cloud.instance`` record
        :returns: the dismissed records (empty when there was nothing)
        """
        active = as_platform(self).search([
            ("state", "=", "active"),
            ("instance_id", "=", instance.id),
        ])
        if active:
            active.write({"state": "dismissed", "resolution": "auto"})
            for record in active:
                record._notify_alert_resolved()
        return active

    def _notify_alert_resolved(self):
        """Announce this alert's automatic resolution where it was raised.

        Every channel that announced the alert announces its end: the
        external ones (Telegram, webhook) and the email. An alert that
        reached someone's inbox and closed on its own otherwise only
        left the active list, and the panel, opened later, showed
        nothing of what the email had said.
        """
        self.ensure_one()
        if self.audience == "provider" and self._route_provider_alert(
            resolved=True,
        ):
            return
        self._notify_alert_external(resolved=True)
        self._notify_alert_email(resolved=True)

    def _dispatch_notifications(self):
        """Central dispatcher for every active alert.

        This is the single point through which ALL alert notifications
        flow — bus broadcast, email, Telegram, and webhooks. Any code
        that creates a ``cloud.alert`` gets full-channel delivery
        without needing to know which transports exist.

        Critical alerts always bypass digest-mode gating. A provider
        alert the panel's provider layer takes elsewhere (see
        :meth:`_route_provider_alert`) is still broadcast — the panel
        shows it — but pages nobody here.
        """
        critical = self.level == "critical"
        self._broadcast_overview(critical=self if critical else self.browse())
        if self.audience == "provider" and self._route_provider_alert():
            return
        self._notify_alert_external()
        self._notify_alert_email()

    def _route_provider_alert(self, resolved=False):
        """Take a provider alert to the provider; return whether it went.

        Core knows nobody but this panel's operator, so by default
        nothing goes anywhere and the operator is notified as usual:
        on a self-hosted install they *are* the provider. A layer that
        has one (the tenant module, whose panel is hosted by
        IncubaCloud) overrides this to hand the alert over and returns
        True, which keeps it out of the operator's inbox and channels.

        :param bool resolved: the alert just closed on its own, rather
            than opened
        :returns: True when the provider was told and local
            notifications must be skipped
        """
        self.ensure_one()
        return False

    @api.model
    def _provider_alerts_routed(self):
        """Whether provider alerts leave this panel for a provider.

        The model-level twin of :meth:`_route_provider_alert`, for the
        readers that select alerts in bulk rather than notify one — the
        daily digest must not list in the morning what the provider was
        told about the night before. Core has no provider: False.

        :returns: True when a provider layer takes provider alerts
        """
        return False

    def write(self, vals):
        # The alerts this write closes, to stamp how and when. A close
        # that does not say how is the platform's (an executor, a cron,
        # a condition that cleared); the panel's own buttons say
        # ``manual``.
        closing = (
            self.filtered(lambda a: a.state == "active")
            if vals.get("state") == "dismissed" else self.browse()
        )
        res = super().write(vals)
        if closing:
            super(CloudAlert, closing).write({
                "resolution": vals.get("resolution") or "auto",
                "resolved_at": fields.Datetime.now(),
            })
            # A failure that held its target's job queue is closed: what
            # waited behind it may start now.
            self.env["cloud.job"]._on_holding_alert_closed(closing)
        # Only broadcast on state transitions — field-level edits
        # (message tweaks, metadata) do not change the overview badge.
        if "state" in vals:
            self._broadcast_overview()
        return res

    def unlink(self):
        # Capture the env before the rows go away so the broadcast
        # can still hit all active internal users.
        env = self.env
        res = super().unlink()
        self.browse()._broadcast_overview_from(env)
        return res

    # NOTE: alerts may be global (no host / instance / project) — used
    # for system-wide security or platform events (OIDC code reuse,
    # GitHub App credentials nearing expiry, JWKS rotation overdue,
    # etc.). Visibility of those rows is restricted at the record-rule
    # layer: ``rule_alert_member`` excludes targetless rows from
    # stakeholders/consultants, so only project-managers+ see them.

    @api.model
    def _gc(self, older_than_days=60):
        """Drop dismissed alerts older than N days. Cron-invoked.

        Active alerts are never touched — active means unresolved, no
        matter how old. Dismissed rows are resolved history and would
        otherwise grow without bound (nothing else deletes them).
        """
        cutoff = fields.Datetime.now() - timedelta(days=older_than_days)
        self.sudo().search(
            [
                ("state", "=", "dismissed"),
                ("write_date", "<", cutoff),
            ]
        ).unlink()
        self.env["cloud.alert.suspicion"].sudo()._gc()

    def _broadcast_overview(self, critical=None):
        """Notify every active internal user that the alert overview may
        have changed — they will refetch ``/cloud/get_alert_count`` and
        ``/cloud/get_dashboard`` through the normal ACL path.

        The payload is empty by default: the bus does not respect
        ``ir.rule`` filters, so we never ship alert data blindly through
        it. The one exception is *critical* alerts: the client toasts
        them live, so each user's event carries the subset of new
        critical alerts that user is allowed to read — visibility is
        checked per user before the send, keeping the record rules
        authoritative.
        """
        self._broadcast_overview_from(self.env, critical=critical)

    @api.model
    def _broadcast_overview_from(self, env, critical=None):
        users = env["res.users"].search(
            [
                ("share", "=", False),
                ("active", "=", True),
            ]
        )
        for user in users:
            payload = {}
            if critical:
                visible = critical.filtered(
                    lambda a, u=user: self._alert_visible_to(a, u)
                    and not self._alert_muted_for(a, u),
                )
                if visible:
                    payload = {
                        "critical": [
                            {"id": a.id, "message": a.message} for a in visible.sudo()
                        ]
                    }
            user._bus_send("cloud_overview", payload)

    @api.model
    def _alert_visible_to(self, alert, user):
        """Return True when *user* passes ACL + record rules to read
        *alert* — gates what travels on their bus channel.
        """
        try:
            alert.with_user(user).check_access("read")
        except AccessError:
            return False
        return True

    @api.model
    def _alert_muted_for(self, alert, user):
        """Return True when the alert's project is muted by *user* —
        muted projects never toast, though they stay in the panel.
        """
        project = alert.project_id or (
            alert.instance_id.project_id if alert.instance_id else None
        )
        return user._cloud_project_muted(project)

    @api.model
    def _alert_user_eligible(self, user, channel="email"):
        """Return True when *user* should receive this alert on *channel*.

        Encapsulates the notification-level + channel-toggle logic so the
        email and external paths stay in sync. ``failures`` lets through
        failed jobs and alerts that are, or have been, critical;
        ``failures_warnings`` and ``all`` let every alert through.
        """
        if user.cloud_notification_level == "none":
            return False
        if channel == "email" and not user.cloud_email_enabled:
            return False
        if user.cloud_notification_level == "failures":
            # The worst the alert was, not its level now: whoever was
            # paged when it turned critical is told when it is over,
            # even if it stepped down to a warning before clearing.
            if self.code != "job_failed" and not (
                self.level == "critical" or self.was_critical
            ):
                return False
        return True

    @api.model
    def action_dismiss_all(self, level_filter=None):
        """Dismiss every active, non-blocking alert visible to the
        current user and return how many were affected.

        Conflict alerts (``pip_conflict`` / ``addon_conflict``) are
        excluded — they gate deploys and have their own Resolve flow,
        so a bulk sweep must never silence them. Record rules scope
        the search; the write access check covers the rest.
        """
        domain = [
            ("state", "=", "active"),
            ("code", "not in", ("pip_conflict", "addon_conflict")),
        ]
        if level_filter in ("warning", "critical"):
            domain.append(("level", "=", level_filter))
        alerts = self.search(domain)
        if alerts:
            alerts.check_access("write")
            alerts.write({"state": "dismissed", "resolution": "manual"})
        return len(alerts)

    # ── Unified notification pipeline ──────────────────────────────────

    def _notify_alert_email(self, resolved=False):
        """Send alert email to every applicable internal user.

        Filtering mirrors ``cloud_job._notify_by_email``: subscribed
        internal users who can see the alert via record rules, haven't
        muted its project, and whose notification level covers the
        alert's severity. Digest-mode users only receive critical
        alerts immediately.

        :param bool resolved: announce the alert's automatic resolution
            instead, to the same users the alert itself reached.
        """
        users = self._alert_notification_users()
        if not users:
            return
        critical = self.level == "critical"
        if not critical:
            users = users.filtered(
                lambda u: u.cloud_notification_mode != "daily_digest",
            )
        for user in users:
            if not self._alert_user_eligible(user, channel="email"):
                continue
            email = user.partner_id.email
            if not email:
                continue
            level_label = self.level.capitalize()
            # Every panel of a fleet mails the same operator mailbox,
            # so the subject says which one is talking.
            panel = self._panel_host()
            tag = f"[IncubaCloud] {panel} ·" if panel else "[IncubaCloud]"
            subject = (
                f"{tag} Resolved: {self.message}" if resolved
                else f"{tag} {level_label} alert: {self.message}"
            )
            body = wrap_in_mail_layout(
                self.env, self._build_alert_email_body(resolved=resolved),
            )
            self.env["mail.mail"].sudo().create(
                {
                    "subject": subject,
                    "body_html": body,
                    "email_to": email,
                    "auto_delete": True,
                }
            ).send()

    def _notify_alert_external(self, resolved=False):
        """Push alert (or its resolution) to Telegram/webhook channels.

        Runs once per applicable internal user. With ``resolved=True``
        the same routing/eligibility applies but the message announces
        the closure — a channel that saw the raise sees the resolve.

        Telegram and webhook sends each run inside try/except so one
        broken endpoint never starves another user's notification.

        In test mode the pipeline is disabled by default — real
        HTTP calls would spill credentials to external APIs.  Tests
        that intentionally exercise the pipeline set a context flag:
        ``with_context(test_external_notify=True)``.
        """
        if odoo_config.get("test_enable") and not self.env.context.get(
            "test_external_notify"
        ):
            return
        users = self._alert_notification_users()
        if not users:
            return
        critical = self.level == "critical"
        if not critical:
            users = users.filtered(
                lambda u: u.cloud_notification_mode != "daily_digest",
            )
        for user in users:
            if not self._alert_user_eligible(user, channel="external"):
                continue
            if user.cloud_telegram_bot_token and user.cloud_telegram_chat_id:
                try:
                    self._send_alert_telegram(user, resolved=resolved)
                except Exception:
                    _logger.warning(
                        "alert telegram send failed for user %s",
                        user.id,
                    )
            if user.cloud_webhook_url:
                try:
                    self._send_alert_webhook(user, resolved=resolved)
                except Exception:
                    _logger.warning(
                        "alert webhook send failed for user %s",
                        user.id,
                    )

    def _alert_notification_users(self):
        """Return the user set eligible to receive alert notifications.

        Active internal users subscribed at any level — visibility and
        mute are checked per-alert, not here, so callers can reuse this
        set across multiple alerts.
        """
        return (
            self.with_context(active_test=False)
            .env["res.users"]
            .search(
                [
                    ("share", "=", False),
                    ("active", "=", True),
                    ("cloud_notification_level", "!=", "none"),
                ]
            )
            .filtered(
                lambda u: self._alert_visible_to(self, u)
                and not self._alert_muted_for(self, u)
            )
        )

    def _send_alert_telegram(self, user, resolved=False):
        """POST an alert (or resolution) notification to Telegram."""
        if resolved:
            emoji, label = "✅", "Resolved"
        else:
            emoji = "\U0001f534" if self.level == "critical" else "\U0001f7e1"
            label = "Alert"
        lines = [
            f"{emoji} *{label}:* {self.message}",
        ]
        if resolved and self.resolved_by_job_id:
            lines.append(f"Resolved by: {self.resolved_by_job_id.name}")
        if self.instance_id:
            lines.append(f"Instance: {self.instance_id.name}")
        if self.host_id:
            lines.append(f"Host: {self.host_id.name}")
        if self.job_id:
            base_url = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param(
                    "web.base.url",
                    "",
                )
            )
            lines.append(f"Logs: {base_url}/cloud/log/{self.job_id.id}")
        if resolved and self.resolved_by_job_id:
            lines.append(
                f"Resolving run: {base_url}/cloud/log/{self.resolved_by_job_id.id}"
            )
        text = "\n".join(lines)
        url = f"https://api.telegram.org/bot{user.cloud_telegram_bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": user.cloud_telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
        ).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        safe_urlopen(req, timeout=10)  # nosec B310 — Telegram API, no redirects

    def _send_alert_webhook(self, user, resolved=False):
        """POST a signed JSON alert notification to the user's webhook URL.

        ``resolved=True`` sends the same payload shape with
        ``event: "alert_resolved"`` so consumers can close the incident
        they opened on the ``alert`` event.
        """
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "web.base.url",
                "",
            )
        )
        payload = json.dumps(
            {
                "event": "alert_resolved" if resolved else "alert",
                "alert_id": self.id,
                "code": self.code,
                "message": self.message,
                "level": self.level,
                "instance": self.instance_id.name if self.instance_id else None,
                "host": self.host_id.name if self.host_id else None,
                "job_id": self.job_id.id if self.job_id else None,
                "log_url": (
                    f"{base_url}/cloud/log/{self.job_id.id}" if self.job_id else None
                ),
                "resolved_by_job_id": (
                    self.resolved_by_job_id.id
                    if resolved and self.resolved_by_job_id else None
                ),
                "timestamp": fields.Datetime.to_string(fields.Datetime.now()),
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        secret = user.cloud_webhook_secret or ""
        if secret:
            sig = hmac.new(
                secret.encode(),
                payload,
                hashlib.sha256,
            ).hexdigest()
            headers["X-IncubaCloud-Signature"] = "sha256=" + sig
        # User-chosen destination — see :mod:`..net.outbound`. The
        # request leaves a machine that can reach the metadata service
        # and the internal database, so the URL is re-resolved and the
        # socket pinned to the address that passed.
        post_json(user.cloud_webhook_url, payload, headers=headers)

    def _build_alert_email_body(self, resolved=False):
        """Render a simple HTML body for an alert email.

        :param bool resolved: render the resolution's email, which also
            names the job whose success closed the alert, if any.
        """
        # Everything interpolated is text, never markup: the message and
        # the names come from wherever the alert was raised — since the
        # provider relay, from a customer's panel — and a link forged
        # into an instance name must not be live in an operator's mail.
        message = escape(self.message)
        instance_line = (
            f"<tr><td><b>Instance</b></td><td>{escape(self.instance_id.name)}</td></tr>"
            if self.instance_id
            else ""
        )
        host_line = (
            f"<tr><td><b>Host</b></td><td>{escape(self.host_id.name)}</td></tr>"
            if self.host_id
            else ""
        )
        job_line = (
            f"<tr><td><b>Job</b></td><td>{escape(self.job_id.name)}</td></tr>"
            if self.job_id
            else ""
        )
        panel = escape(self._panel_host())
        panel_line = (
            f"<tr><td><b>Panel</b></td><td>{panel}</td></tr>" if panel else ""
        )
        # Absolute, like the Telegram and webhook links: a relative
        # path opens nothing from a mail client.
        base_url = self._base_url()
        log_url = f"{base_url}/cloud/log/{self.job_id.id}" if self.job_id else ""
        level_label = self.level.capitalize()
        resolving = self.resolved_by_job_id if resolved else self.resolved_by_job_id.browse()
        resolving_line = (
            f"<tr><td><b>Resolved by</b></td><td>"
            f"<a href='{base_url}/cloud/log/{resolving.id}'>{escape(resolving.name)}</a></td></tr>"
            if resolving
            else ""
        )
        headline = (
            f"<p><b>Resolved:</b> {message}</p>" if resolved
            else f"<p><b>{level_label} alert:</b> {message}</p>"
        )
        return (
            headline
            + f"<table>"
            f"{panel_line}"
            f"{instance_line}"
            f"{host_line}"
            f"{job_line}"
            f"{resolving_line}"
            f"</table>"
            + (f"<p><a href='{log_url}'>View logs</a></p>" if log_url else "")
        )

    @api.model
    def _base_url(self):
        """Return this panel's ``web.base.url`` without a trailing slash."""
        return (
            self.env["ir.config_parameter"].sudo()
            .get_param("web.base.url", "") or ""
        ).rstrip("/")

    @api.model
    def _panel_host(self):
        """Return the host name of this panel, or '' when unknown.

        It is what tells one panel's mail from another's in a mailbox
        that receives them all, so it is the host of ``web.base.url``
        rather than a company name every panel shares.
        """
        return urllib.parse.urlsplit(self._base_url()).netloc


class CloudAlertSuspicion(models.Model):
    """A condition seen, but not yet often enough to be an alert.

    One row per (code, target), bumped by ``cloud.alert.raise_alert``
    on each sighting and dropped the moment the alert is raised or the
    condition clears. It is deliberately not a ``cloud.alert`` in a
    special state: nothing that lists, counts, notifies or garbage-
    collects alerts has to know suspicions exist.
    """

    _name = "cloud.alert.suspicion"
    _description = "Alert Suspicion"

    code = fields.Char(required=True, index=True)
    host_id = fields.Many2one("cloud.host", ondelete="cascade")
    instance_id = fields.Many2one("cloud.instance", ondelete="cascade")
    occurrences = fields.Integer(
        default=1,
        help="Sightings in a row so far, the last within the window.",
    )
    last_seen_at = fields.Datetime(required=True)

    # For the reason ``cloud_alert_active_identity_uidx`` exists: two
    # producers under snapshot isolation cannot see each other's row, so
    # only the database can keep one count per target.
    _target_uniq = models.UniqueIndex(
        "(code, COALESCE(host_id, 0), COALESCE(instance_id, 0))",
        "One suspicion per alert code and target.",
    )

    #: Sightings older than this are not worth keeping: no window a
    #: producer uses comes close, and a condition that stops being seen
    #: has nothing to confirm.
    _STALE_AFTER = timedelta(days=1)

    @staticmethod
    def _target_domain(code, host, instance):
        """Return the domain identifying the suspicion for (code, target)."""
        return [
            ("code", "=", code),
            ("host_id", "=", host.id if host else False),
            ("instance_id", "=", instance.id if instance else False),
        ]

    @api.model
    def _sight(self, code, host, instance, confirm_after, window):
        """Record one sighting; return True when it is the confirming one.

        The confirming sighting also removes the row, so the alert that
        follows it starts from a clean count. A sighting older than
        *window* seconds is forgotten and the count starts over at one.

        :param str code: alert code being confirmed
        :param host: ``cloud.host`` target, if any
        :param instance: ``cloud.instance`` target, if any
        :param int confirm_after: sightings in a row that confirm
        :param int window: seconds a previous sighting stays fresh
        :returns: whether the caller should now raise the alert
        """
        self = self.sudo()
        now = fields.Datetime.now()
        row = self.search(self._target_domain(code, host, instance), limit=1)
        if row:
            fresh = (now - row.last_seen_at).total_seconds() <= window
            count = row.occurrences + 1 if fresh else 1
            if count >= confirm_after:
                row.unlink()
                return True
            row.write({"occurrences": count, "last_seen_at": now})
            return False
        try:
            with self.env.cr.savepoint():
                self.create({
                    "code": code,
                    "host_id": host.id if host else False,
                    "instance_id": instance.id if instance else False,
                    "occurrences": 1,
                    "last_seen_at": now,
                })
        except psycopg2.errors.UniqueViolation:
            # A concurrent producer filed the first sighting; this one
            # is the same sighting, not a second one.
            pass
        return False

    @api.model
    def _forget(self, code, host, instance):
        """Drop the suspicion for (code, target), if any."""
        self.sudo().search(self._target_domain(code, host, instance)).unlink()

    @api.model
    def _gc(self):
        """Drop suspicions nobody has refreshed for a day."""
        cutoff = fields.Datetime.now() - self._STALE_AFTER
        self.sudo().search([("last_seen_at", "<", cutoff)]).unlink()


class CloudAlertPolicy(models.Model):
    """How many sightings in a row make a measured condition an alert.

    One row per alert code, or per family of codes when the code carries
    a ``*`` (``instance_service_*_down``). ``cloud.alert.raise_alert``
    reads it for every code whose caller did not bring a count of its
    own, so the producers that measure — the health probe, the SSH disk
    probe, the metric-rule cron's backend check — stay unaware of the
    rule, and which alerts confirm is read from this table rather than
    from their code. Shipped rows are data, not ``noupdate``: tuning
    knobs that follow the module. A code with no row is an event and
    raises on its first sighting.
    """

    _name = "cloud.alert.policy"
    _description = "Alert Confirmation Policy"
    _order = "code"

    code = fields.Char(
        required=True,
        index=True,
        help="Alert code as the producer raises it. A trailing or "
             "embedded '*' matches a family of codes; an exact row wins "
             "over a pattern.",
    )
    confirm_after = fields.Integer(
        string="Confirm after",
        default=2,
        required=True,
        help="Sightings in a row, each within the window of the last, "
             "before the alert exists and anyone is told. 1 raises on "
             "the first sighting.",
    )
    confirm_window = fields.Integer(
        string="Window (seconds)",
        default=900,
        required=True,
        help="How long a sighting stays fresh. The next one must come "
             "within this many seconds or the count starts over. Three "
             "ticks of the producer's schedule is the usual value.",
    )
    note = fields.Char(
        help="Why this code is a measurement and what one lone reading "
             "would have cost.",
    )

    _code_uniq = models.Constraint(
        "UNIQUE(code)", "One confirmation policy per alert code.",
    )

    #: What ``_for_code`` answers for a code nobody listed.
    _DEFAULT = (1, 900)

    @api.constrains("confirm_after", "confirm_window")
    def _check_counts(self):
        """Refuse a policy that could never confirm."""
        for policy in self:
            if policy.confirm_after < 1:
                raise ValidationError(_(
                    "A policy must confirm after at least one sighting.",
                ))
            if policy.confirm_window < 1:
                raise ValidationError(_(
                    "A policy's window must be at least one second.",
                ))

    @api.model
    def _for_code(self, code):
        """Return ``(confirm_after, confirm_window)`` for *code*.

        An exact row wins; otherwise the first pattern row, in code
        order, that matches; otherwise the default of one sighting.

        :param str code: the alert code about to be raised
        :returns: a ``(sightings, seconds)`` tuple
        """
        rows = self.sudo().search_fetch([], ["code", "confirm_after", "confirm_window"])
        exact = rows.filtered(lambda r: r.code == code)
        if exact:
            return exact[0].confirm_after, exact[0].confirm_window
        for row in rows:
            if "*" in row.code and fnmatch.fnmatchcase(code, row.code):
                return row.confirm_after, row.confirm_window
        return self._DEFAULT

