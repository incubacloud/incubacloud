"""Integration test for queue_job_ext.QueueJob.write.

Specifically verifies that the raw UPDATE performed on the cloud_job
table invalidates the ORM cache for ``state`` — otherwise subsequent
reads in the same transaction (broadcast, notifications, computes)
would see the previous value.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


class TestQueueJobExtInvalidatesCache(TransactionCase):

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'qj-host',
            'ip_address': '10.0.0.42',
            'user': 'ubuntu',
            'wildcard_domain': 'qj.example.com',
        })
        self.jt = self._job_type('qj_test')
        self.cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self.jt.id,
            'name': 'QJ Test',
            'queue_job_uuid': 'test-uuid-1',
        })

    def test_terminal_state_transition_invalidates_cache(self):
        """After queue.job.write({'state': 'done'}), a later read of
        cloud.job.state in the same transaction returns the fresh
        value. Regression guard for the raw UPDATE bypass."""
        # Seed the queue.job with a non-terminal state so our write
        # transitions it to 'done' and triggers the ext hook.
        qjob = self.env['queue.job'].sudo().create({
            'uuid': 'test-uuid-1',
            'name': 'qj-test',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        # Prime the ORM cache with the stale value.
        self.assertEqual(self.cjob.state, 'pending')

        # queue_job_ext.write runs: does raw UPDATE on cloud_job,
        # then invalidate_model(['state']).
        qjob.write({'state': 'done'})

        # If invalidate_model is missing, the cache would still
        # return 'pending'. With the fix it yields 'done'.
        self.assertEqual(self.cjob.state, 'done')


class TestQueueJobExtFailureAlert(TransactionCase):
    """Failure of a queue.job opens a ``cloud.alert`` so the failure
    lingers in the Alerts panel beyond the one-shot bus toast."""

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'alert-host',
            'ip_address': '10.0.0.43',
            'user': 'ubuntu',
            'wildcard_domain': 'alert.example.com',
        })

    def _make(self, code, uuid, apply_to='host'):
        jt = self._job_type(code, apply_to=apply_to)
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': f'Job {code}',
            'queue_job_uuid': uuid,
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{code}',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        return cjob, qjob

    def test_failure_creates_warning_alert_for_non_severe_type(self):
        cjob, qjob = self._make('host_probe', 'uuid-warn')
        qjob.write({
            'state': 'failed',
            'exc_message': 'ssh: Network is unreachable',
        })
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('code', '=', 'job_failed'),
        ])
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'warning')
        self.assertEqual(alert.state, 'active')
        self.assertIn('host_probe', alert.message.lower())
        self.assertIn('Network is unreachable', alert.message)

    def test_cancelling_queued_job_syncs_cancelled_without_alert(self):
        # A queued (not-yet-started) job cancelled via cancel_job goes
        # through the queue.job (button_cancelled). queue_job_ext must sync
        # cloud.job.state to 'cancelled' and raise neither a failure alert
        # nor a failure email (cancelled is a documented non-event).
        cjob, qjob = self._make('deploy_instance', 'uuid-cancel-queued')
        qjob.write({'state': 'cancelled'})
        self.env['cloud.job'].invalidate_model(['state'])
        self.assertEqual(cjob.state, 'cancelled')
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('code', '=', 'job_failed'),
        ])
        self.assertFalse(alert, "cancellation must not raise a failure alert")

    def test_failure_creates_critical_alert_for_severe_type(self):
        cjob, qjob = self._make('deploy_instance', 'uuid-crit')
        qjob.write({
            'state': 'failed',
            'exc_message': 'docker: image pull timeout',
        })
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('code', '=', 'job_failed'),
        ])
        self.assertEqual(alert.level, 'critical')

    def test_failure_in_hidden_type_does_not_create_alert(self):
        """Background cron types (host_metrics, docker_prune, …)
        self-recover and would flood the panel; never alerted."""
        cjob, qjob = self._make('host_metrics', 'uuid-hidden')
        qjob.write({
            'state': 'failed', 'exc_message': 'transient failure',
        })
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id),
        ])
        self.assertFalse(alert)

    def test_retry_success_dismisses_previous_alert(self):
        """When the same cloud.job reaches 'done' after a failure the
        active job_failed alert is auto-dismissed."""
        cjob, qjob = self._make('rebuild_instance', 'uuid-retry')
        qjob.write({'state': 'failed', 'exc_message': 'oops'})
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob.id), ('state', '=', 'active'),
        ])
        self.assertEqual(len(alert), 1)

        # Reuse the same cloud.job on retry — flip state back to
        # enqueued and then to done, same uuid.
        qjob.write({'state': 'enqueued'})
        qjob.write({'state': 'done'})

        alert.invalidate_recordset()
        self.assertEqual(alert.state, 'dismissed')

    def test_new_job_success_dismisses_stale_alert_same_type_target(self):
        """``retry_job`` creates a fresh cloud.job, so a success must
        dismiss the *original* failed job's alert too (same job type,
        same target) — otherwise it lingers active forever."""
        cjob_a, qjob_a = self._make('rebuild_instance', 'uuid-stale-a')
        qjob_a.write({'state': 'failed', 'exc_message': 'boom'})
        alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob_a.id), ('state', '=', 'active'),
        ])
        self.assertEqual(len(alert), 1)

        # A brand-new job of the same type on the same host succeeds.
        cjob_b, qjob_b = self._make('rebuild_instance', 'uuid-stale-b')
        qjob_b.write({'state': 'done'})

        alert.invalidate_recordset()
        self.assertEqual(alert.state, 'dismissed')

    def test_success_keeps_other_type_alerts_active(self):
        """A success only clears alerts of its own job type/target —
        an unrelated failure on the same host must stay visible."""
        cjob_probe, qjob_probe = self._make('host_probe', 'uuid-keep-a')
        qjob_probe.write({'state': 'failed', 'exc_message': 'nope'})
        probe_alert = self.env['cloud.alert'].search([
            ('job_id', '=', cjob_probe.id), ('state', '=', 'active'),
        ])
        self.assertEqual(len(probe_alert), 1)

        cjob_reb, qjob_reb = self._make('rebuild_instance', 'uuid-keep-b')
        qjob_reb.write({'state': 'done'})

        probe_alert.invalidate_recordset()
        self.assertEqual(probe_alert.state, 'active')


@tagged('post_install', '-at_install')
class TestJobEmailNotificationFilters(TransactionCase):
    """``_notify_by_email`` must skip hidden job types and cancelled
    jobs — only user-visible success/failure events reach the inbox.

    ``post_install``: setUp creates a res.users, which crashes at
    at_install on the ``res_partner.autopost_bills`` NOT NULL column
    (the ``account`` module owning it loads later).
    """

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'mail-host',
            'ip_address': '10.0.0.44',
            'user': 'ubuntu',
            'wildcard_domain': 'mail.example.com',
        })
        # A DEDICATED subscriber that wants every event and can read
        # host-level jobs (project-manager group → rule_job_all). Do
        # not lean on self.env.user: who that is, and which groups /
        # email it carries, differs between devel and the CI runner —
        # the reason the positive-control test flaked only in CI.
        self.subscriber = self.env['res.users'].create({
            'name': 'mail-subscriber',
            'login': 'mail-subscriber',
            'email': 'ops@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
        })
        self.subscriber.cloud_notification_level = 'all'

    def _sub_mail_count(self):
        """Job-notification mails delivered to the dedicated subscriber."""
        return self.env['mail.mail'].sudo().search_count([
            ('subject', 'like', '[IncubaCloud] Job%'),
            ('email_to', '=', 'ops@example.com'),
        ])

    def _job(self, code):
        """Create a cloud.job of *code* on the test host."""
        return self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self._job_type(code).id,
            'name': f'Mail {code}',
            'queue_job_uuid': f'uuid-mail-{code}',
        })

    def _mail_count(self):
        """Count outgoing job-notification mails."""
        return self.env['mail.mail'].sudo().search_count([
            ('subject', 'like', '[IncubaCloud] Job%'),
        ])

    def test_hidden_type_failure_sends_no_email(self):
        job = self._job('host_metrics')
        before = self._mail_count()
        self.env['cloud.job']._notify_by_email(job, 'failed')
        self.assertEqual(self._mail_count(), before)

    def test_cancelled_sends_no_email(self):
        job = self._job('host_probe')
        before = self._mail_count()
        self.env['cloud.job']._notify_by_email(job, 'cancelled')
        self.assertEqual(self._mail_count(), before)

    def test_visible_failure_still_sends_email(self):
        """Positive control: the filters must not silence real events —
        the dedicated subscriber receives the failure mail."""
        job = self._job('host_probe')
        before = self._sub_mail_count()
        self.env['cloud.job']._notify_by_email(job, 'failed')
        self.assertGreater(self._sub_mail_count(), before)


class TestJobRequiresActionFlag(TransactionCase):
    """``_format`` flags a job as requiring action only for blocking
    conflict alerts — a plain failure just needs a Retry."""

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'fmt-host',
            'ip_address': '10.0.0.45',
            'user': 'ubuntu',
            'wildcard_domain': 'fmt.example.com',
        })
        self.cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self._job_type('rebuild_instance').id,
            'name': 'Fmt job',
            'queue_job_uuid': 'uuid-fmt-1',
        })

    def test_job_failed_alert_does_not_require_action(self):
        self.env['cloud.alert'].sudo().create({
            'code': 'job_failed',
            'message': 'Rebuild failed',
            'host_id': self.host.id,
            'job_id': self.cjob.id,
        })
        fmt = self.cjob._format()[0]
        self.assertFalse(fmt['requires_action'])
        self.assertNotIn('has_alert', fmt)

    def test_actionable_alert_requires_action(self):
        self.env['cloud.alert'].sudo().create({
            'code': 'addon_conflict',
            'message': 'Addon defined in multiple repos',
            'host_id': self.host.id,
            'job_id': self.cjob.id,
        })
        fmt = self.cjob._format()[0]
        self.assertTrue(fmt['requires_action'])

    def test_blocked_job_requires_action(self):
        alert = self.env['cloud.alert'].sudo().create({
            'code': 'pip_conflict',
            'message': 'Pip conflict',
            'host_id': self.host.id,
        })
        self.cjob.write({'blocked_alert_id': alert.id})
        fmt = self.cjob._format()[0]
        self.assertTrue(fmt['requires_action'])
        self.assertEqual(fmt['state'], 'blocked')


@tagged('post_install', '-at_install')
class TestJobEmailScoping(TransactionCase):
    """Job emails must mirror the cloud.job record rules: a user who
    cannot read the job in the UI must not receive its emails.

    ``post_install``: creating res.users at_install crashes on the
    ``res_partner.autopost_bills`` NOT NULL column — the ``account``
    module that owns the field loads after ``incubacloud``, so its
    Python default is not in the registry yet (same quirk documented
    for the cron bot in ``res_users_ext``).
    """

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def _user(self, login, group_xmlids):
        """Create an internal user subscribed to every job email."""
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'email': f'{login}@example.com',
        })
        gids = [self.env.ref('base.group_user').id]
        gids += [self.env.ref(x).id for x in group_xmlids]
        user.write({
            'group_ids': [(4, gid) for gid in gids],
            'cloud_notification_level': 'all',
        })
        return user

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'scope-host',
            'ip_address': '10.0.0.46',
            'user': 'ubuntu',
            'wildcard_domain': 'scope.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'Scope Proj'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'scope-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.member = self._user(
            'scope-member', ['incubacloud.group_cloud_consultant'],
        )
        self.outsider = self._user(
            'scope-outsider', ['incubacloud.group_cloud_consultant'],
        )
        self.manager = self._user(
            'scope-manager', ['incubacloud.group_cloud_project_manager'],
        )
        self.project.write({'member_ids': [(4, self.member.id)]})

    def _job(self, with_instance=True):
        """Create a visible (non-hidden) job, optionally instance-scoped."""
        vals = {
            'host_id': self.host.id,
            'job_type_id': self._job_type('host_probe').id,
            'name': 'Scope job',
            'queue_job_uuid': f'uuid-scope-{with_instance}',
        }
        if with_instance:
            vals['instance_id'] = self.instance.id
        return self.env['cloud.job'].sudo().create(vals)

    def _recipients(self):
        return set(self.env['mail.mail'].sudo().search([
            ('subject', 'like', '[IncubaCloud] Job%'),
        ]).mapped('email_to'))

    def test_instance_job_emails_member_and_manager_not_outsider(self):
        self.env['cloud.job']._notify_by_email(self._job(), 'failed')
        recipients = self._recipients()
        self.assertIn('scope-member@example.com', recipients)
        self.assertIn('scope-manager@example.com', recipients)
        self.assertNotIn('scope-outsider@example.com', recipients)

    def test_host_job_emails_manager_only(self):
        """Host-level jobs are manager territory per rule_job_member —
        even project members must not be emailed about them."""
        self.env['cloud.job']._notify_by_email(
            self._job(with_instance=False), 'failed',
        )
        recipients = self._recipients()
        self.assertIn('scope-manager@example.com', recipients)
        self.assertNotIn('scope-member@example.com', recipients)
        self.assertNotIn('scope-outsider@example.com', recipients)

    def test_muted_project_skips_email(self):
        """Muting a project silences its job emails for that user only."""
        self.member.write({
            'cloud_muted_project_ids': [(4, self.project.id)],
        })
        self.env['cloud.job']._notify_by_email(self._job(), 'failed')
        recipients = self._recipients()
        self.assertNotIn('scope-member@example.com', recipients)
        # Everyone else is unaffected by a personal mute.
        self.assertIn('scope-manager@example.com', recipients)

    def test_daily_digest_skips_non_severe(self):
        """Digest users get no instant email for ordinary failures —
        those belong to the daily digest."""
        self.manager.write({'cloud_notification_mode': 'daily_digest'})
        self.env['cloud.job']._notify_by_email(
            self._job(with_instance=False), 'failed',
        )
        self.assertNotIn('scope-manager@example.com', self._recipients())

    def test_daily_digest_gets_severe_failure_immediately(self):
        """Severe failures (production deploys/rebuilds) always skip
        the digest and land in the inbox at once."""
        self.manager.write({'cloud_notification_mode': 'daily_digest'})
        job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self._job_type('rebuild_instance').id,
            'instance_id': self.instance.id,
            'name': 'Severe job',
            'queue_job_uuid': 'uuid-scope-severe',
        })
        self.env['cloud.job']._notify_by_email(job, 'failed')
        self.assertIn('scope-manager@example.com', self._recipients())

    def _qfail(self, code, uuid, instance=False):
        """Create a cloud.job + queue.job pair and drive it to failed.

        Mirrors the production ``enqueue`` order: the uuid is written
        AFTER the queue.job exists, so the stored related ``state``
        links properly. Writing it at create time would leave a
        pending recompute against an empty ``queue_job_id`` that a
        later flush resolves to NULL, clobbering the raw-SQL 'failed'
        (the stored-related trap documented for Odoo 18).
        """
        vals = {
            'host_id': self.host.id,
            'job_type_id': self._job_type(code).id,
            'name': f'Digest {code}',
        }
        if instance:
            vals['instance_id'] = self.instance.id
        cjob = self.env['cloud.job'].sudo().create(vals)
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{uuid}',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        cjob.write({'queue_job_uuid': uuid})
        qjob.write({'state': 'failed', 'exc_message': 'digest boom'})
        return cjob

    def _digest_mails(self):
        return self.env['mail.mail'].sudo().search([
            ('subject', 'like', '[IncubaCloud] Daily digest%'),
        ])

    def _fresh_watermark(self):
        """Narrow the digest window to the last minute so pre-existing
        devel data can never leak into the assertions."""
        return fields.Datetime.now() - timedelta(seconds=60)

    def test_digest_sends_failed_jobs(self):
        self.manager.write({
            'cloud_notification_mode': 'daily_digest',
            'cloud_last_digest_at': self._fresh_watermark(),
        })
        self._qfail('host_probe', 'uuid-digest-1')
        self.env['res.users']._cron_send_cloud_digest()
        mails = self._digest_mails().filtered(
            lambda m: m.email_to == 'scope-manager@example.com',
        )
        self.assertEqual(len(mails), 1)
        self.assertIn('1 failed job(s)', mails.subject)
        # Watermark advanced past the job's terminal write.
        self.assertTrue(self.manager.cloud_last_digest_at)

    def test_digest_empty_window_sends_nothing(self):
        now = fields.Datetime.now()
        self.manager.write({
            'cloud_notification_mode': 'daily_digest',
            'cloud_last_digest_at': now,
        })
        before = len(self._digest_mails())
        self.env['res.users']._cron_send_cloud_digest()
        self.assertEqual(len(self._digest_mails()), before)
        # Watermark still advanced — empty windows stay bounded.
        self.assertGreaterEqual(self.manager.cloud_last_digest_at, now)

    def test_digest_respects_muted_projects(self):
        self.manager.write({
            'cloud_notification_mode': 'daily_digest',
            'cloud_last_digest_at': self._fresh_watermark(),
            'cloud_muted_project_ids': [(4, self.project.id)],
        })
        self._qfail('host_probe', 'uuid-digest-mute', instance=True)
        before = len(self._digest_mails())
        self.env['res.users']._cron_send_cloud_digest()
        self.assertEqual(len(self._digest_mails()), before)

    def test_digest_includes_new_alerts_without_jobs(self):
        self.manager.write({
            'cloud_notification_mode': 'daily_digest',
            'cloud_last_digest_at': self._fresh_watermark(),
        })
        self.env['cloud.alert'].sudo().create({
            'code': 'digest_alert_t',
            'level': 'critical',
            'message': 'Digest alert body',
            'host_id': self.host.id,
        })
        self.env['res.users']._cron_send_cloud_digest()
        mails = self._digest_mails().filtered(
            lambda m: m.email_to == 'scope-manager@example.com',
        )
        self.assertEqual(len(mails), 1)
        self.assertIn('1 new alert(s)', mails.subject)
        self.assertIn('Digest alert body', mails.body_html)

    def test_alert_muted_for_follows_project(self):
        """The mute helper resolves the alert's project directly or
        through its instance."""
        Alert = self.env['cloud.alert']
        direct = Alert.sudo().create({
            'code': 'mute_t1', 'message': 'm',
            'project_id': self.project.id,
        })
        via_inst = Alert.sudo().create({
            'code': 'mute_t2', 'message': 'm',
            'instance_id': self.instance.id,
        })
        self.assertFalse(Alert._alert_muted_for(direct, self.member))
        self.member.write({
            'cloud_muted_project_ids': [(4, self.project.id)],
        })
        self.assertTrue(Alert._alert_muted_for(direct, self.member))
        self.assertTrue(Alert._alert_muted_for(via_inst, self.member))
        self.assertFalse(Alert._alert_muted_for(direct, self.manager))


class TestJobFailedAlertDedup(TransactionCase):
    """One broken thing is one alert, however many times it retries.

    ``_create_job_failed_alert`` called ``create`` straight out, so a
    cron job that kept failing stacked a row *and* a mail/Telegram/
    webhook per attempt. On 2026-08-24 a broken metrics ACL sync failed
    every thirty minutes for six and a half hours: fourteen identical
    criticals, fourteen notifications, all dismissed together by the
    same later success. Everything else in the panel was buried under
    it.
    """

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search([('code', '=', code)], limit=1)
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'dedup-job-host',
            'ip_address': '10.0.0.45',
            'user': 'ubuntu',
            'wildcard_domain': 'dedupjob.example.com',
        })

    def _fail(self, code, uuid, message='boom'):
        jt = self._job_type(code)
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': f'Job {code}',
            'queue_job_uuid': uuid,
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{code}',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        qjob.write({'state': 'failed', 'exc_message': message})
        return cjob

    def _alerts(self, **extra):
        domain = [
            ('code', '=', 'job_failed'),
            ('host_id', '=', self.host.id),
            ('state', '=', 'active'),
        ]
        domain += [(k, '=', v) for k, v in extra.items()]
        return self.env['cloud.alert'].search(domain)

    def test_repeated_failure_refreshes_one_alert(self):
        first = self._fail('sync_metrics_accounts', 'uuid-d1', 'rc=2')
        second = self._fail('sync_metrics_accounts', 'uuid-d2', 'rc=2 again')
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.occurrences, 2)
        # The row points at the newest failure, so "open the log" lands
        # on the attempt that is still failing.
        self.assertEqual(alerts.job_id, second)
        self.assertNotEqual(alerts.job_id, first)
        self.assertIn('rc=2 again', alerts.message)
        self.assertIn('×2', alerts.message)

    def test_a_different_job_type_is_a_different_incident(self):
        self._fail('sync_metrics_accounts', 'uuid-d3')
        self._fail('host_probe', 'uuid-d4')
        self.assertEqual(len(self._alerts()), 2)

    def test_success_dismisses_and_the_next_failure_starts_over(self):
        self._fail('sync_metrics_accounts', 'uuid-d5')
        self._fail('sync_metrics_accounts', 'uuid-d6')
        self.assertEqual(self._alerts().occurrences, 2)

        jt = self._job_type('sync_metrics_accounts')
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': 'Job ok',
            'queue_job_uuid': 'uuid-d7',
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': 'uuid-d7',
            'name': 'qj-ok',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        qjob.write({'state': 'done'})
        self.assertFalse(self._alerts())
        self.assertTrue(cjob)

        self._fail('sync_metrics_accounts', 'uuid-d8')
        reopened = self._alerts()
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened.occurrences, 1)


class TestHostUnreachableOnTerminalState(TransactionCase):
    """``host_unreachable`` belongs to the job that actually gave up.

    ``execute`` used to raise it on the attempt it judged to be the
    last, from the retry counter. The runner can hand a job one more
    attempt than the budget, so that judgement was wrong: on
    2026-08-29 three critical alerts fired for jobs that connected
    seconds later and finished ``done``. Hanging it off the terminal
    state makes "it gave up" a fact.
    """

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search([('code', '=', code)], limit=1)
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'unreach-host',
            'ip_address': '10.0.0.46',
            'user': 'ubuntu',
            'wildcard_domain': 'unreach.example.com',
        })

    def _terminal(self, code, uuid, state, exc_message, retry=10):
        jt = self._job_type(code)
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': f'Job {code}',
            'queue_job_uuid': uuid,
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{code}',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        qjob.write({
            'state': state, 'exc_message': exc_message, 'retry': retry,
        })
        return cjob

    def _alert(self):
        return self.env['cloud.alert'].search([
            ('code', '=', 'host_unreachable'),
            ('host_id', '=', self.host.id),
            ('state', '=', 'active'),
        ])

    def test_exhausted_connection_retries_alert(self):
        from odoo.addons.incubacloud.models.abstract_executor import (
            CONNECTION_RETRY_MARKER,
        )
        self._terminal(
            'host_metrics', 'uuid-u1', 'failed',
            f'Max. retries (10) reached: {CONNECTION_RETRY_MARKER} 10/10: '
            f'TimeoutError: ',
        )
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'critical')
        self.assertIn('10 consecutive', alert.message)
        # The operator reads the exception, not queue_job's wrapper.
        self.assertTrue(alert.message.endswith('Last error: TimeoutError:'))
        self.assertNotIn('Max. retries', alert.message)

    def test_failure_for_another_reason_says_nothing_about_the_host(self):
        self._terminal(
            'host_metrics', 'uuid-u2', 'failed', 'KeyError: no such thing',
        )
        self.assertFalse(self._alert())

    def test_a_job_type_that_does_not_retry_connections_never_alerts(self):
        from odoo.addons.incubacloud.models.abstract_executor import (
            CONNECTION_RETRY_MARKER,
        )
        self._terminal(
            'deploy_instance', 'uuid-u3', 'failed',
            f'Max. retries (1) reached: {CONNECTION_RETRY_MARKER} 1/1: x',
        )
        self.assertFalse(self._alert())

    def test_a_job_that_finished_never_alerts(self):
        self._terminal('host_metrics', 'uuid-u4', 'done', False)
        self.assertFalse(self._alert())
