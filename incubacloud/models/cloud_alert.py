import hashlib
import hmac
import json
import logging
import urllib.request
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)


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
    message = fields.Char(required=True)
    level = fields.Selection(
        [("warning", "Warning"), ("critical", "Critical")],
        default="warning",
        required=True,
    )
    state = fields.Selection(
        [("active", "Active"), ("dismissed", "Dismissed")],
        default="active",
        required=True,
    )
    create_date = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records.filtered(
            lambda r: r.state == "active",
        ):
            record._dispatch_notifications()
        return records

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
    def _dedup_domain(code, host=None, instance=None):
        """Return the domain identifying the active alert for a target.

        Identity is (code, host, instance, active). An alert about a host
        and one about an instance are different alerts even with the same
        code, which is what lets a per-instance rule fan out without
        clobbering the host-level one.
        """
        return [
            ("code", "=", code),
            ("state", "=", "active"),
            ("host_id", "=", host.id if host else False),
            ("instance_id", "=", instance.id if instance else False),
        ]

    @api.model
    def raise_alert(self, code, message, level="warning",
                    host=None, instance=None, job=None):
        """Create or refresh the active alert for (code, target).

        Idempotent: a repeated call updates the message/level of the
        existing alert instead of creating a second one, so a cron that
        re-evaluates every few minutes does not flood the operator.

        :param str code: machine-readable dedup identifier
        :param str message: human-readable text shown in the panel
        :param str level: ``warning`` or ``critical``
        :param host: ``cloud.host`` this alert is about, if any
        :param instance: ``cloud.instance`` this alert is about, if any
        :param job: ``cloud.job`` that produced it, if any
        :returns: the active ``cloud.alert`` record
        """
        existing = self.search(
            self._dedup_domain(code, host=host, instance=instance), limit=1,
        )
        vals = {"message": message, "level": level}
        if job:
            vals["job_id"] = job.id
        if existing:
            existing.write(vals)
            return existing
        return self.create({
            "code": code,
            "host_id": host.id if host else False,
            "instance_id": instance.id if instance else False,
            **vals,
        })

    @api.model
    def resolve_alert(self, code, host=None, instance=None):
        """Dismiss the active alert for (code, target), if any.

        Idempotent: resolving something that was never raised is a no-op,
        so callers do not have to track whether they alerted before.

        :returns: the dismissed records (empty when there was nothing)
        """
        active = self.search(
            self._dedup_domain(code, host=host, instance=instance),
        )
        if active:
            active.write({"state": "dismissed"})
            # On-call channels saw the problem; they must also see the
            # closure, or every incident looks permanently open from
            # Telegram/webhook. Only *automatic* resolution notifies:
            # an operator dismissing by hand in the panel is already
            # looking at it and chose to silence it.
            for record in active:
                record._notify_alert_external(resolved=True)
        return active

    def _dispatch_notifications(self):
        """Central dispatcher for every active alert.

        This is the single point through which ALL alert notifications
        flow — bus broadcast, email, Telegram, and webhooks. Any code
        that creates a ``cloud.alert`` gets full-channel delivery
        without needing to know which transports exist.

        Critical alerts always bypass digest-mode gating.
        """
        critical = self.level == "critical"
        self._broadcast_overview(critical=self if critical else self.browse())
        self._notify_alert_external()
        self._notify_alert_email()

    def write(self, vals):
        res = super().write(vals)
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
        email and external paths stay in sync.
        """
        if user.cloud_notification_level == "none":
            return False
        if channel == "email" and not user.cloud_email_enabled:
            return False
        if user.cloud_notification_level == "failures":
            if self.code != "job_failed" and self.level != "critical":
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
            alerts.write({"state": "dismissed"})
        return len(alerts)

    # ── Unified notification pipeline ──────────────────────────────────

    def _notify_alert_email(self):
        """Send alert email to every applicable internal user.

        Filtering mirrors ``cloud_job._notify_by_email``: subscribed
        internal users who can see the alert via record rules, haven't
        muted its project, and whose notification level covers the
        alert's severity. Digest-mode users only receive critical
        alerts immediately.
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
            subject = f"[IncubaCloud] {level_label} alert: {self.message}"
            body = self._build_alert_email_body()
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
        urllib.request.urlopen(req, timeout=10)  # nosec B310

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
        req = urllib.request.Request(
            user.cloud_webhook_url,
            data=payload,
            headers=headers,
        )
        urllib.request.urlopen(req, timeout=10)  # nosec B310

    def _build_alert_email_body(self):
        """Render a simple HTML body for an alert email."""
        instance_line = (
            f"<tr><td><b>Instance</b></td><td>{self.instance_id.name}</td></tr>"
            if self.instance_id
            else ""
        )
        host_line = (
            f"<tr><td><b>Host</b></td><td>{self.host_id.name}</td></tr>"
            if self.host_id
            else ""
        )
        job_line = (
            f"<tr><td><b>Job</b></td><td>{self.job_id.name}</td></tr>"
            if self.job_id
            else ""
        )
        log_url = f"/cloud/log/{self.job_id.id}" if self.job_id else ""
        level_label = self.level.capitalize()
        return (
            f"<p><b>{level_label} alert:</b> {self.message}</p>"
            f"<table>"
            f"{instance_line}"
            f"{host_line}"
            f"{job_line}"
            f"</table>"
            + (f"<p><a href='{log_url}'>View logs</a></p>" if log_url else "")
        )
