"""What the failed-job toast is told about the job.

The toast used to say "<job> failed — check the job log": not on what,
not why, and the job log it pointed to sat under the toast itself. The
brief now carries the target and the same one-line excerpt the
failed-job alert shows, so the toast can say both and open the job
history of that target. The access gates in front of it must not move.
"""
import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase


class TestFailureExcerpt(TransactionCase):
    """One line, short, shared by the alert and the toast."""

    def test_nothing_to_show_is_an_empty_string(self):
        Job = self.env['cloud.job']
        self.assertEqual(Job._failure_excerpt(None), '')
        self.assertEqual(Job._failure_excerpt(''), '')

    def test_only_the_first_line_is_kept(self):
        excerpt = self.env['cloud.job']._failure_excerpt(
            '  no stock for cx23 in hel1\nTraceback (most recent call last):\n'
        )
        self.assertEqual(excerpt, 'no stock for cx23 in hel1')

    def test_a_long_line_is_capped(self):
        excerpt = self.env['cloud.job']._failure_excerpt('x' * 300)
        self.assertEqual(len(excerpt), 100)


@tagged('-at_install', 'post_install')
class TestJobBrief(HttpCase):
    """Driven over real HTTP: the route's access gates are the point."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'brief-host',
            'ip_address': '10.0.0.46',
            'user': 'ubuntu',
            'wildcard_domain': 'brief.example.com',
        })
        self.job_type = self.env['cloud.job.type'].create({
            'name': 'Brief job', 'code': 'brief_job', 'apply_to': 'host',
        })
        self._make_user(
            'brief-pm',
            ['base.group_user', 'incubacloud.group_cloud_project_manager'],
        )
        self._make_user(
            'brief-outsider',
            ['base.group_user', 'incubacloud.group_cloud_user'],
        )

    def _make_user(self, login, groups):
        """Create a user with a known password for ``authenticate``.

        :param login: login and password (same string, test-only).
        :param groups: xml ids of the groups to grant.
        :returns: the created ``res.users`` record.
        """
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'password': login,
            'group_ids': [(6, 0, [self.env.ref(g).id for g in groups])],
        })

    def _job(self, uuid, final_state, exc_message=None):
        """A host job whose queue job ended in *final_state*.

        :param uuid: queue job uuid, unique per test.
        :param final_state: ``done`` or ``failed``.
        :param exc_message: exception text stored on a failed queue job.
        :returns: the ``cloud.job``, already in its terminal state.
        """
        # Queue job first, as ``enqueue`` does: the cloud job's stored
        # link to it is computed from the uuid when the uuid is written.
        qjob = self.env['queue.job'].sudo().create({
            'uuid': uuid,
            'name': f'qj-{uuid}',
            'state': 'pending',
            'method_name': 'noop',
            'model_name': 'cloud.job',
            'func_string': 'noop()',
        })
        cjob = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self.job_type.id,
            'name': 'Brief job',
            'queue_job_uuid': uuid,
        })
        self.assertEqual(cjob.queue_job_id, qjob)
        vals = {'state': final_state}
        if exc_message is not None:
            vals['exc_message'] = exc_message
        qjob.write(vals)
        return cjob

    def _brief(self, job):
        """POST ``/cloud/get_job_brief`` for *job* on the current session.

        :param job: the ``cloud.job`` to ask about.
        :returns: the decoded ``result`` payload.
        """
        payload = {
            'jsonrpc': '2.0', 'method': 'call', 'id': 1,
            'params': {'job_id': job.id},
        }
        return self.url_open(
            '/cloud/get_job_brief',
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
        ).json().get('result')

    def test_a_failed_job_says_on_what_and_why(self):
        job = self._job(
            'brief-failed', 'failed',
            'no stock for cx23 in hel1\nTraceback (most recent call last):',
        )
        self.assertEqual(job.state, 'failed')
        self.authenticate('brief-pm', 'brief-pm')
        brief = self._brief(job)
        self.assertTrue(brief['ok'])
        self.assertEqual(brief['state'], 'failed')
        self.assertEqual(brief['target_name'], 'brief-host')
        self.assertEqual(brief['host_id'], self.host.id)
        self.assertFalse(brief['instance_id'])
        self.assertEqual(brief['error'], 'no stock for cx23 in hel1')

    def test_a_finished_job_carries_no_error(self):
        job = self._job('brief-done', 'done')
        self.authenticate('brief-pm', 'brief-pm')
        brief = self._brief(job)
        self.assertTrue(brief['ok'])
        self.assertEqual(brief['error'], '')
        self.assertEqual(brief['target_name'], 'brief-host')

    def test_a_job_the_caller_cannot_read_still_says_nothing(self):
        job = self._job('brief-hidden', 'failed', 'secret detail')
        self.authenticate('brief-outsider', 'brief-outsider')
        self.assertEqual(self._brief(job), {'ok': False})
