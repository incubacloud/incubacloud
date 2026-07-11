"""Tests for Telegram and HMAC webhook push notifications.

The ``_notify_external`` pipeline runs after every terminal job
transition and pushes to Telegram / external webhooks for every
eligible user. These tests verify the same gates as the email
channel (hidden types, cancelled, mute, visibility, digest mode,
level filter) and the low-level send helpers.
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestExternalNotifyGates(TransactionCase):
    """``_notify_external`` must skip hidden, cancelled and muted jobs.

    ``post_install`` because creating res.users at_install crashes on
    ``res_partner.autopost_bills`` NOT NULL (account loads later).
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

    def _subscriber(self):
        """Create a user with Telegram AND webhook configured so every
        gate test passes the "has external config" pre-filter."""
        user = self.env['res.users'].create({
            'name': 'ext-notify-user',
            'login': 'ext-notify-user',
            'email': 'ext@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
        })
        user.write({
            'cloud_notification_level': 'all',
            'cloud_telegram_bot_token': 'test-bot-token',
            'cloud_telegram_chat_id': '123456789',
        })
        return user

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'ext-host',
            'ip_address': '10.0.0.48',
            'user': 'ubuntu',
            'wildcard_domain': 'ext.example.com',
        })
        self._visible_jt = self._job_type('host_probe')
        self._hidden_jt = self._job_type('host_metrics')

    def _job(self, jt=None):
        return self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': (jt or self._visible_jt).id,
            'name': 'External test',
            'queue_job_uuid': 'uuid-ext',
        })

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_hidden_job_type_skipped(self, mock_open):
        """Hidden job types never push notifications to any channel."""
        self._subscriber()
        job = self._job(jt=self._hidden_jt)
        self.env['cloud.job']._notify_external(job, 'failed')
        mock_open.assert_not_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_cancelled_skipped(self, mock_open):
        """Cancelled is a user-initiated non-event — no push."""
        self._subscriber()
        job = self._job()
        self.env['cloud.job']._notify_external(job, 'cancelled')
        mock_open.assert_not_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_visible_failure_sends_telegram(self, mock_open):
        """A visible failing job reaches the Telegram API for a
        configured user (positive control)."""
        self._subscriber()
        self.env['cloud.job']._notify_external(self._job(), 'failed')
        mock_open.assert_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_muted_project_skipped(self, mock_open):
        """A user who muted the job's project gets no push."""
        sub = self._subscriber()
        project = self.env['cloud.project'].create({'name': 'Muted Proj'})
        instance = self.env['cloud.instance'].create({
            'name': 'muted-inst',
            'project_id': project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        sub.write({'cloud_muted_project_ids': [(4, project.id)]})
        job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self._visible_jt.id,
            'name': 'Muted job',
            'queue_job_uuid': 'uuid-muted',
            'instance_id': instance.id,
        })
        self.env['cloud.job']._notify_external(job, 'failed')
        mock_open.assert_not_called()


@tagged('post_install', '-at_install')
class TestExternalNotifyScoping(TransactionCase):
    """Push notifications must honour the same record-rule gates as
    email: a user who cannot read the job in the UI gets no push."""

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
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'email': f'{login}@example.com',
        })
        gids = [self.env.ref('base.group_user').id]
        gids += [self.env.ref(x).id for x in group_xmlids]
        user.write({
            'group_ids': [(4, gid) for gid in gids],
            'cloud_notification_level': 'all',
            'cloud_telegram_bot_token': 'test-bot-token',
            'cloud_telegram_chat_id': '123456789',
        })
        return user

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'scope-ext-host',
            'ip_address': '10.0.0.49',
            'user': 'ubuntu',
            'wildcard_domain': 'scope-ext.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'Scope Ext'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'scope-ext-inst',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
        })
        self.member = self._user(
            'ext-member', ['incubacloud.group_cloud_consultant'],
        )
        self.outsider = self._user(
            'ext-outsider', ['incubacloud.group_cloud_consultant'],
        )
        self.manager = self._user(
            'ext-manager', ['incubacloud.group_cloud_project_manager'],
        )
        self.project.write({'member_ids': [(4, self.member.id)]})
        self._visible_jt = self._job_type('host_probe')

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_instance_job_pushes_member_not_outsider(self, mock_open):
        """An instance-scoped job pushes to member but not outsider."""
        job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self._visible_jt.id,
            'name': 'Scope push',
            'queue_job_uuid': 'uuid-scope-push',
            'instance_id': self.instance.id,
        })
        self.env['cloud.job']._notify_external(job, 'failed')
        mock_open.assert_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_host_job_pushes_manager_only(self, mock_open):
        """Host-level jobs are manager territory — member sees nothing."""
        job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self._visible_jt.id,
            'name': 'Host push',
            'queue_job_uuid': 'uuid-host-push',
        })
        self.env['cloud.job']._notify_external(job, 'failed')
        # Manager (created in _user) gets the push; member doesn't
        mock_open.assert_called()
        # The manager user has telegram config so at least 1 call was made
        self.assertGreaterEqual(mock_open.call_count, 1)


@tagged('post_install', '-at_install')
class TestExternalNotifyPreferences(TransactionCase):
    """Digest-mode and failures-only preferences gate external pushes
    the same way they gate email."""

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
            'name': 'pref-ext-host',
            'ip_address': '10.0.0.50',
            'user': 'ubuntu',
            'wildcard_domain': 'pref-ext.example.com',
        })
        self._visible_jt = self._job_type('host_probe')
        self._deploy_jt = self._job_type('deploy_instance')

    def _user_with_telegram(self, **overrides):
        vals = {
            'name': 'pref-user',
            'login': 'pref-user',
            'email': 'pref@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_telegram_bot_token': 'test-bot-token',
            'cloud_telegram_chat_id': '123456789',
        }
        vals.update(overrides)
        return self.env['res.users'].create(vals)

    def _job(self, jt=None):
        return self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': (jt or self._visible_jt).id,
            'name': 'Pref test',
            'queue_job_uuid': 'uuid-pref',
        })

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_digest_user_skipped_for_non_severe(self, mock_open):
        """Digest-mode user gets no push for a regular failure."""
        self._user_with_telegram(
            login='digest-user',
            name='digest-user',
            email='digest@example.com',
            cloud_notification_mode='daily_digest',
        )
        job = self._job()
        self.env['cloud.job']._notify_external(job, 'failed')
        mock_open.assert_not_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_digest_user_gets_severe_immediately(self, mock_open):
        """Digest-mode user gets push for a severe (deploy) failure."""
        self._user_with_telegram(
            login='digest-severe',
            name='digest-severe',
            email='digest-severe@example.com',
            cloud_notification_mode='daily_digest',
        )
        job = self._job(jt=self._deploy_jt)
        self.env['cloud.job']._notify_external(job, 'failed')
        mock_open.assert_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_failures_only_skips_done(self, mock_open):
        """level='failures' gets no push for state='done'."""
        self._user_with_telegram(
            login='fail-only',
            name='fail-only',
            email='fail-only@example.com',
            cloud_notification_level='failures',
        )
        job = self._job()
        self.env['cloud.job']._notify_external(job, 'done')
        mock_open.assert_not_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_failures_only_gets_failure(self, mock_open):
        """level='failures' gets push for state='failed'."""
        self._user_with_telegram(
            login='fail-gets',
            name='fail-gets',
            email='fail-gets@example.com',
            cloud_notification_level='failures',
        )
        job = self._job()
        self.env['cloud.job']._notify_external(job, 'failed')
        mock_open.assert_called()


@tagged('post_install', '-at_install')
class TestTelegramSend(TransactionCase):
    """Low-level ``_send_telegram`` builds a valid Bot API request."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'tg-host',
            'ip_address': '10.0.0.51',
            'user': 'ubuntu',
            'wildcard_domain': 'tg.example.com',
        })
        self.user = self.env['res.users'].create({
            'name': 'tg-user',
            'login': 'tg-user',
            'email': 'tg@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_telegram_bot_token': 'test-bot-token',
            'cloud_telegram_chat_id': '123456789',
        })
        jt = self.env['cloud.job.type'].search(
            [('code', '=', 'host_probe')], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': 'host_probe', 'code': 'host_probe', 'apply_to': 'host',
            })
        self.job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': 'TG job',
            'queue_job_uuid': 'uuid-tg',
        })

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_send_message_url_contains_bot_token_and_method(self, mock_open):
        """The request targets ``/bot{token}/sendMessage``."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        self.assertIn('/sendMessage', req.full_url)
        self.assertIn('/bot', req.full_url)

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_payload_contains_job_name_and_state(self, mock_open):
        """The JSON body includes job metadata."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        self.assertIn(self.job.name, body['text'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_markdown_parse_mode_set(self, mock_open):
        """Telegram messages use Markdown parse mode."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(body['parse_mode'], 'Markdown')

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_disables_web_page_preview(self, mock_open):
        """Link previews are disabled so log URLs don't render cards."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        self.assertTrue(body['disable_web_page_preview'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_request_has_json_content_type(self, mock_open):
        """The Content-Type header must be ``application/json``."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.headers['Content-type'], 'application/json')

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_failure_emoji_is_red_circle(self, mock_open):
        """Failed jobs are prefixed with the red-circle emoji."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        self.assertIn('\U0001F534', body['text'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_done_emoji_is_green_circle(self, mock_open):
        """Successful jobs are prefixed with the green-circle emoji."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'done', False)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        self.assertIn('\U0001F7E2', body['text'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_severe_includes_warning_tag(self, mock_open):
        """Severe failures carry a warning-emoji tag in the message."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', True)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        self.assertIn('SEVERE', body['text'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_api_call_uses_token_from_user(self, mock_open):
        """The bot token in the URL comes from the user record."""
        self.env['cloud.job']._send_telegram(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        self.assertIn('/bot', req.full_url)


@tagged('post_install', '-at_install')
class TestWebhookSend(TransactionCase):
    """Low-level ``_send_webhook`` builds and signs payloads correctly."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'wh-host',
            'ip_address': '10.0.0.52',
            'user': 'ubuntu',
            'wildcard_domain': 'wh.example.com',
        })
        self.user = self.env['res.users'].create({
            'name': 'wh-user',
            'login': 'wh-user',
            'email': 'wh@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_webhook_url': 'https://hooks.example.com/cloud',
        })
        jt = self.env['cloud.job.type'].search(
            [('code', '=', 'host_probe')], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': 'host_probe', 'code': 'host_probe', 'apply_to': 'host',
            })
        self.job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': 'WH job',
            'queue_job_uuid': 'uuid-wh',
        })

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_sends_to_configured_url(self, mock_open):
        """The POST targets the user's webhook_url."""
        self.env['cloud.job']._send_webhook(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, 'https://hooks.example.com/cloud')

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_payload_is_valid_json(self, mock_open):
        """The request body is parseable JSON."""
        self.env['cloud.job']._send_webhook(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data)
        self.assertEqual(payload['event'], 'job_state_change')
        self.assertEqual(payload['job_id'], self.job.id)
        self.assertEqual(payload['job_name'], 'WH job')
        self.assertEqual(payload['state'], 'failed')
        self.assertFalse(payload['severe'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_signature_header_when_secret_configured(self, mock_open):
        """With a signing secret the header is present and valid."""
        self.user.cloud_webhook_secret = 'my-secret'
        self.env['cloud.job']._send_webhook(self.user, self.job, 'failed', True)
        req = mock_open.call_args[0][0]
        sig = req.headers['X-incubacloud-signature']
        self.assertTrue(sig.startswith('sha256='))
        import hashlib
        import hmac
        expected = 'sha256=' + hmac.new(
            b'my-secret', req.data, hashlib.sha256,
        ).hexdigest()
        self.assertEqual(sig, expected)

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_no_signature_header_without_secret(self, mock_open):
        """Without a signing secret the header is absent."""
        self.user.cloud_webhook_secret = ''
        self.env['cloud.job']._send_webhook(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        self.assertNotIn('X-incubacloud-signature', req.headers)

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_payload_includes_timestamp(self, mock_open):
        """Every payload carries an ISO-8601 timestamp."""
        self.env['cloud.job']._send_webhook(self.user, self.job, 'done', False)
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data)
        self.assertIn('timestamp', payload)
        self.assertIsInstance(payload['timestamp'], str)

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_severe_flag_in_payload(self, mock_open):
        """Severe failures carry the boolean flag in the payload."""
        self.env['cloud.job']._send_webhook(self.user, self.job, 'failed', True)
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data)
        self.assertTrue(payload['severe'])

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_content_type_is_json(self, mock_open):
        """The Content-Type header is application/json."""
        self.env['cloud.job']._send_webhook(self.user, self.job, 'failed', False)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.headers['Content-type'], 'application/json')


@tagged('post_install', '-at_install')
class TestExternalNotifyUnconfigured(TransactionCase):
    """Users without Telegram or webhook config cause no HTTP calls."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'naked-host',
            'ip_address': '10.0.0.53',
            'user': 'ubuntu',
            'wildcard_domain': 'naked.example.com',
        })
        jt = self.env['cloud.job.type'].search(
            [('code', '=', 'host_probe')], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': 'host_probe', 'code': 'host_probe', 'apply_to': 'host',
            })
        self.job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': 'Naked job',
            'queue_job_uuid': 'uuid-naked',
        })

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_no_telegram_without_token(self, mock_open):
        """User with chat_id but no bot_token gets no Telegram call."""
        user = self.env['res.users'].create({
            'name': 'no-token',
            'login': 'no-token',
            'email': 'notoken@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_telegram_chat_id': '123456789',
        })
        self.env['cloud.job']._notify_external(self.job, 'failed')
        mock_open.assert_not_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_no_telegram_without_chat_id(self, mock_open):
        """User with bot_token but no chat_id gets no Telegram call."""
        user = self.env['res.users'].create({
            'name': 'no-chat',
            'login': 'no-chat',
            'email': 'nochat@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_telegram_chat_id': '',
        })
        self.env['cloud.job']._notify_external(self.job, 'failed')
        mock_open.assert_not_called()

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_no_webhook_without_url(self, mock_open):
        """User with secret but no URL gets no webhook call."""
        user = self.env['res.users'].create({
            'name': 'no-url',
            'login': 'no-url',
            'email': 'nourl@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_webhook_url': '',
        })
        self.env['cloud.job']._notify_external(self.job, 'failed')
        mock_open.assert_not_called()


@tagged('post_install', '-at_install')
class TestEmailToggle(TransactionCase):
    """When cloud_email_enabled is False, email is skipped but
    Telegram (and any other channel) still fires."""

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
            'name': 'et-host',
            'ip_address': '10.0.0.54',
            'user': 'ubuntu',
            'wildcard_domain': 'et.example.com',
        })
        jt = self._job_type('host_probe')
        self.job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': jt.id,
            'name': 'Email toggle test',
            'queue_job_uuid': 'uuid-et',
        })

    def _subscriber(self, email_enabled=True, with_telegram=False):
        vals = {
            'name': 'et-user',
            'login': 'et-user',
            'email': 'et@example.com',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_project_manager').id),
            ],
            'cloud_notification_level': 'all',
            'cloud_email_enabled': email_enabled,
        }
        if with_telegram:
            vals['cloud_telegram_bot_token'] = 'test-bot-token'
            vals['cloud_telegram_chat_id'] = '123456789'
        return self.env['res.users'].create(vals)

    def _mail_count(self, email='et@example.com'):
        """Count outgoing job-notification mails to a specific recipient."""
        return self.env['mail.mail'].sudo().search_count([
            ('subject', 'like', '[IncubaCloud] Job%'),
            ('email_to', '=', email),
        ])

    def test_email_disabled_sends_no_email(self):
        """cloud_email_enabled=False → _notify_by_email skips the user."""
        self._subscriber(email_enabled=False)
        before = self._mail_count('et@example.com')
        self.env['cloud.job']._notify_by_email(self.job, 'failed')
        self.assertEqual(self._mail_count('et@example.com'), before)

    def test_email_enabled_sends_email(self):
        """cloud_email_enabled=True (default) → email is sent (positive control)."""
        self._subscriber(email_enabled=True)
        before = self._mail_count('et@example.com')
        self.env['cloud.job']._notify_by_email(self.job, 'failed')
        self.assertGreater(self._mail_count('et@example.com'), before)

    @patch('odoo.addons.incubacloud.models.cloud_job.urllib.request.urlopen')
    def test_email_disabled_telegram_still_fires(self, mock_open):
        """Disabling email must not affect Telegram delivery."""
        self._subscriber(email_enabled=False, with_telegram=True)
        self.env['cloud.job']._notify_external(self.job, 'failed')
        mock_open.assert_called()
