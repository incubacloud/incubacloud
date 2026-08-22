"""Tier 2 — alert notification delivery through ``_dispatch_notifications``.

The unification commit (c4ae52c) routed failed-job notifications through
``cloud.alert.create()`` → ``_dispatch_notifications()``, but the alert
path's user filtering diverged from the job path's in three ways fixed here:

* ``cloud_email_enabled`` was not checked — a user disabling email still
  received alert emails.
* ``failures``-only users received only ``critical`` alerts, dropping
  non-severe ``job_failed`` warnings — a regression from the old job-path
  behaviour where every job failure reached ``failures``-subscribed users.
* The digest cron ignored ``cloud_email_enabled`` and included
  ``job_failed`` alerts redundantly alongside the already-listed failed
  jobs, plus showed all alert levels to ``failures``-only users instead
  of only critical infrastructure alerts.

These tests drive the alert NOTIFICATION path (``_notify_alert_email`` /
``_notify_alert_external``), NOT the old ``_notify_by_email(job, failed)``
path that is no longer used for failures in production.
"""

from datetime import timedelta
from unittest.mock import patch

from psycopg2 import sql as psql

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAlertNotifyEmail(TransactionCase):
    """Alert email delivery respects user preferences the same way the
    old job path did — ``cloud_email_enabled``, ``failures``-only, and
    the special case that ``job_failed`` alerts are job failures, not
    generic infrastructure conditions."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "alert-email-host",
                "ip_address": "10.0.0.60",
                "user": "ubuntu",
                "wildcard_domain": "alert-email.example.com",
            }
        )
        # Neutralise admin's Telegram / webhook config so
        # _notify_alert_external never makes real HTTP calls.
        self.env["res.users"].sudo().browse(2).write(
            {
                "cloud_telegram_bot_token": "",
                "cloud_telegram_chat_id": "",
                "cloud_webhook_url": "",
            }
        )

    def _user(self, login, **overrides):
        vals = {
            "name": login,
            "login": login,
            "email": f"{login}@example.com",
            "group_ids": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("incubacloud.group_cloud_project_manager").id),
            ],
            "cloud_notification_level": "all",
        }
        vals.update(overrides)
        return self.env["res.users"].create(vals)

    def _alert(self, code="job_failed", level="warning", **extra):
        vals = {
            "code": code,
            "level": level,
            "message": f"Test alert {code}",
            "host_id": self.host.id,
        }
        vals.update(extra)
        return self.env["cloud.alert"].sudo().create(vals)

    def _alert_mails(self, email=None):
        domain = [("subject", "like", "[IncubaCloud]%alert%")]
        if email:
            domain.append(("email_to", "=", email))
        return self.env["mail.mail"].sudo().search(domain)

    # ── failures-only: job_failed special case (C3 fix) ──────────

    def test_job_failed_warning_email_to_failures_user(self):
        """REGRESSION: a 'failures'-only user must receive email for
        non-severe job_failed (level=warning) — before the fix the alert
        path's 'self.level != critical' check silently dropped these."""
        self._user("ane-fail", cloud_notification_level="failures")
        before = len(self._alert_mails("ane-fail@example.com"))
        self._alert(code="job_failed", level="warning")
        self.assertGreater(
            len(self._alert_mails("ane-fail@example.com")),
            before,
        )

    def test_job_failed_critical_email_to_failures_user(self):
        """Positive control: severe job_failed (critical) must still
        reach 'failures' users."""
        self._user("ane-crit", cloud_notification_level="failures")
        before = len(self._alert_mails("ane-crit@example.com"))
        self._alert(code="job_failed", level="critical")
        self.assertGreater(
            len(self._alert_mails("ane-crit@example.com")),
            before,
        )

    def test_non_job_warning_alert_no_email_to_failures_user(self):
        """'failures' user must NOT receive warning non-job alerts
        (e.g. pip_conflict) — only critical infrastructure alerts."""
        self._user("ane-no-warn", cloud_notification_level="failures")
        before = len(self._alert_mails("ane-no-warn@example.com"))
        self._alert(code="pip_conflict", level="warning")
        self.assertEqual(
            len(self._alert_mails("ane-no-warn@example.com")),
            before,
        )

    def test_non_job_critical_alert_email_to_failures_user(self):
        """'failures' user must receive critical infrastructure alerts
        (e.g. disk_critical)."""
        self._user("ane-crit-ok", cloud_notification_level="failures")
        before = len(self._alert_mails("ane-crit-ok@example.com"))
        self._alert(code="disk_critical", level="critical")
        self.assertGreater(
            len(self._alert_mails("ane-crit-ok@example.com")),
            before,
        )

    # ── all-level user ────────────────────────────────────────────

    def test_all_user_gets_warning_alert_email(self):
        """'all'-level user must receive every alert, including
        warning-level non-job alerts."""
        self._user("ane-all", cloud_notification_level="all")
        before = len(self._alert_mails("ane-all@example.com"))
        self._alert(code="pip_conflict", level="warning")
        self.assertGreater(
            len(self._alert_mails("ane-all@example.com")),
            before,
        )

    # ── cloud_email_enabled toggle (C1 fix) ───────────────────────

    def test_email_disabled_blocks_alert_email(self):
        """cloud_email_enabled=False must suppress alert emails —
        the old alert path missed this check entirely."""
        self._user(
            "ane-no-email",
            cloud_notification_level="all",
            cloud_email_enabled=False,
        )
        before = len(self._alert_mails("ane-no-email@example.com"))
        self._alert(code="disk_critical", level="critical")
        self.assertEqual(
            len(self._alert_mails("ane-no-email@example.com")),
            before,
        )

    def test_email_enabled_sends_alert_email(self):
        """Positive control: cloud_email_enabled=True (default) must
        deliver alert emails."""
        self._user("ane-email", cloud_notification_level="all")
        before = len(self._alert_mails("ane-email@example.com"))
        self._alert(code="disk_critical", level="critical")
        self.assertGreater(
            len(self._alert_mails("ane-email@example.com")),
            before,
        )


@tagged("post_install", "-at_install")
class TestAlertNotifyExternal(TransactionCase):
    """Telegram and webhook delivery for alerts must honour the same
    'failures'-only semantics as email — job_failed warnings always
    reach 'failures' users (C3 regression fix), and the email toggle
    must not cross over to the independent external channels."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "alert-ext-host",
                "ip_address": "10.0.0.61",
                "user": "ubuntu",
                "wildcard_domain": "alert-ext.example.com",
            }
        )
        self.env["res.users"].sudo().browse(2).write(
            {
                "cloud_telegram_bot_token": "",
                "cloud_telegram_chat_id": "",
                "cloud_webhook_url": "",
            }
        )

    def _user(self, login, **overrides):
        vals = {
            "name": login,
            "login": login,
            "email": f"{login}@example.com",
            "group_ids": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("incubacloud.group_cloud_project_manager").id),
            ],
            "cloud_notification_level": "all",
            "cloud_telegram_bot_token": "test-bot-token",
            "cloud_telegram_chat_id": "123456789",
        }
        vals.update(overrides)
        return self.env["res.users"].create(vals)

    def _alert(self, **extra):
        vals = {
            "code": "job_failed",
            "level": "warning",
            "message": "Test alert ext",
            "host_id": self.host.id,
        }
        vals.update(extra)
        return (
            self.env["cloud.alert"]
            .sudo()
            .with_context(
                test_external_notify=True,
            )
            .create(vals)
        )

    @patch("odoo.addons.incubacloud.models.cloud_alert.safe_urlopen")
    def test_job_failed_warning_telegram_to_failures_user(self, mock_open):
        """REGRESSION: 'failures' user with Telegram must get push for
        non-severe job_failed alerts too."""
        self._user("aext-fail-tg", cloud_notification_level="failures")
        self._alert(code="job_failed", level="warning")
        mock_open.assert_called()

    @patch("odoo.addons.incubacloud.models.cloud_alert.safe_urlopen")
    def test_email_disabled_does_not_block_telegram(self, mock_open):
        """cloud_email_enabled is an email-only toggle — Telegram and
        other external channels must still deliver."""
        self._user(
            "aext-no-email-tg",
            cloud_notification_level="all",
            cloud_email_enabled=False,
        )
        self._alert(code="disk_critical", level="critical")
        mock_open.assert_called()

    @patch("odoo.addons.incubacloud.models.cloud_alert.safe_urlopen")
    def test_external_notify_gated_in_test_mode(self, mock_open):
        """Without context flag, alert creation must not call Telegram."""
        self._user(
            "aext-gated",
            cloud_notification_level="all",
        )
        self.env["cloud.alert"].sudo().create(
            {
                "code": "disk_critical",
                "level": "critical",
                "message": "Test alert ext gated",
                "host_id": self.host.id,
            }
        )
        mock_open.assert_not_called()


class TestAlertNotifyDigest(TransactionCase):
    """Daily digest: job_failed alerts are excluded (the job section
    already covers them), alert level filtering matches the immediate
    path, and cloud_email_enabled must gate the cron."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "digest-alert-host",
                "ip_address": "10.0.0.62",
                "user": "ubuntu",
                "wildcard_domain": "digest-alert.example.com",
            }
        )
        self.env["res.users"].sudo().browse(2).write(
            {
                "cloud_telegram_bot_token": "",
                "cloud_telegram_chat_id": "",
                "cloud_webhook_url": "",
            }
        )

    def _user(self, login, **overrides):
        # TestAlertNotifyDigest sorts alphabetically first among
        # incubacloud's test classes, so these are the very first
        # res.users.create() calls in the whole module's test run —
        # before account's autopost_bills default (required, contributed
        # to res.partner via _inherit) is resolvable by the ORM. Same gap
        # _incubacloud_ensure_cron_bot works around in res_users_ext.py:
        # pre-create the partner via raw SQL with that column filled,
        # then create res.users directly against it.
        self.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'res_partner'"
        )
        partner_cols = {row[0] for row in self.env.cr.fetchall()}
        now = fields.Datetime.now()
        cols = [
            "name",
            "email",
            "active",
            "is_company",
            "type",
            "create_date",
            "write_date",
            "create_uid",
            "write_uid",
        ]
        partner_vals = [
            login,
            f"{login}@example.com",
            True,
            False,
            "contact",
            now,
            now,
            self.env.uid,
            self.env.uid,
        ]
        if "autopost_bills" in partner_cols:
            cols.append("autopost_bills")
            partner_vals.append("ask")
        query = psql.SQL(
            "INSERT INTO res_partner ({cols}) VALUES ({placeholders}) RETURNING id"
        ).format(
            cols=psql.SQL(", ").join(map(psql.Identifier, cols)),
            placeholders=psql.SQL(", ").join([psql.Placeholder()] * len(partner_vals)),
        )
        self.env.cr.execute(query, partner_vals)
        partner_id = self.env.cr.fetchone()[0]
        self.env["res.partner"].invalidate_model()

        vals = {
            "login": login,
            "partner_id": partner_id,
            "group_ids": [
                (4, self.env.ref("base.group_user").id),
                (4, self.env.ref("incubacloud.group_cloud_project_manager").id),
            ],
            "cloud_notification_level": "all",
            "cloud_notification_mode": "daily_digest",
            "cloud_last_digest_at": (fields.Datetime.now() - timedelta(seconds=60)),
        }
        vals.update(overrides)
        return self.env["res.users"].create(vals)

    def _alert(self, **extra):
        vals = {
            "code": "job_failed",
            "level": "warning",
            "message": "Test digest alert",
            "host_id": self.host.id,
        }
        vals.update(extra)
        return self.env["cloud.alert"].sudo().create(vals)

    def _qfail(self, code, uuid):
        """Create a cloud.job + queue.job pair and drive it to failed
        so the digest's job section has content."""
        jt = self.env["cloud.job.type"].search(
            [("code", "=", code)],
            limit=1,
        )
        if not jt:
            jt = self.env["cloud.job.type"].create(
                {
                    "name": code,
                    "code": code,
                    "apply_to": "host",
                }
            )
        cjob = (
            self.env["cloud.job"]
            .sudo()
            .create(
                {
                    "host_id": self.host.id,
                    "job_type_id": jt.id,
                    "name": f"Digest {code}",
                }
            )
        )
        qjob = (
            self.env["queue.job"]
            .sudo()
            .create(
                {
                    "uuid": uuid,
                    "name": f"qj-{uuid}",
                    "state": "pending",
                    "method_name": "noop",
                    "model_name": "cloud.job",
                    "func_string": "noop()",
                }
            )
        )
        cjob.write({"queue_job_uuid": uuid})
        qjob.write({"state": "failed", "exc_message": "digest boom"})
        return cjob

    def _digest_mails(self, login):
        email = f"{login}@example.com"
        return (
            self.env["mail.mail"]
            .sudo()
            .search(
                [
                    ("subject", "like", "[IncubaCloud] Daily digest%"),
                    ("email_to", "=", email),
                ]
            )
        )

    def test_digest_excludes_job_failed_alerts(self):
        """M2 fix: job_failed alerts must NOT appear in the digest's
        alert section — the job section already lists every failure
        with its log URL."""
        self._user("adg-m2")
        # Create a failed job so the digest has something to send
        # (the job_failed alert alone would produce an empty window).
        self._qfail("host_probe", "uuid-digest-m2")
        self._alert(code="job_failed", level="warning")
        self.env["res.users"]._cron_send_cloud_digest()
        mails = self._digest_mails("adg-m2")
        self.assertEqual(len(mails), 1)
        self.assertIn("1 failed job(s)", mails.subject)
        self.assertNotIn("job_failed", mails.body_html)

    def test_digest_alert_level_filter_for_failures_user(self):
        """M3 fix: 'failures'-only digest users must only see critical
        infrastructure alerts — warning non-job alerts are excluded,
        matching the immediate-path behaviour."""
        self._user("adg-m3", cloud_notification_level="failures")
        self._alert(code="pip_conflict", level="warning")
        before = len(self._digest_mails("adg-m3"))
        self.env["res.users"]._cron_send_cloud_digest()
        self.assertEqual(len(self._digest_mails("adg-m3")), before)

    def test_digest_respects_email_enabled(self):
        """C2 fix: cloud_email_enabled=False must suppress the digest
        email entirely — the old cron search missed this filter."""
        self._user(
            "adg-c2",
            cloud_notification_level="all",
            cloud_email_enabled=False,
        )
        self._alert(code="disk_critical", level="critical")
        before = len(self._digest_mails("adg-c2"))
        self.env["res.users"]._cron_send_cloud_digest()
        self.assertEqual(len(self._digest_mails("adg-c2")), before)
