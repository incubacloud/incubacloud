"""A GitHub push merges into the rebuild of a push already waiting.

Replaces the pending-push queue (``cloud.instance.pending.push``, the
one-minute cooldown and the coalesced follow-up the rebuild's
``on_success`` queued): a push always queues its rebuild through
``cloud.job.enqueue``, where ``RebuildInstanceExecutor._merge_queued_payload``
folds it into the push-triggered rebuild waiting on the instance, if
any. Every push stays visible in the job (``coalesced_pushes``) and the
head — the push the job list shows — is the first one.
"""
import json

from odoo.tests.common import TransactionCase

from ..models.cloud_job import _webhook_fields
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from .test_cloud_job import _run_after_commit


def _push_payload(clone_url, branch, sha, message="fix: something"):
    """A push webhook body for *sha* on *branch*."""
    return json.dumps({
        'ref': f'refs/heads/{branch}',
        'deleted': False,
        'after': sha,
        'repository': {'clone_url': clone_url, 'full_name': 'owner/repo'},
        'pusher': {'name': 'dev'},
        'head_commit': {'message': message},
    })


class _PushCase(TransactionCase):

    REPO_URL = 'https://github.com/owner/repo.git'
    BRANCH = 'main'

    def setUp(self):
        super().setUp()
        # For the dispatcher's cursor (see ``_run_after_commit``).
        self.registry_enter_test_mode()
        self.host = self.env['cloud.host'].create({
            'name': 'merge-host',
            'ip_address': '10.0.0.91',
            'user': 'ubuntu',
            'wildcard_domain': 'merge.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'Merge'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'merge-staging',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'auto_rebuild': True,
            'state': 'deployed',
        })
        self.env['cloud.instance.repo'].create({
            'instance_id': self.instance.id,
            'url': self.REPO_URL,
            'branch': self.BRANCH,
        })
        self.Job = self.env['cloud.job']
        self.rebuild_type = self.env['cloud.job.type'].search(
            [('code', '=', 'rebuild_instance')], limit=1,
        )
        self.pushes = 0

    def _push(self, sha=None, message=None):
        """Process a push of *sha*; return the instance's rebuild jobs."""
        self.pushes += 1
        sha = sha or f'sha{self.pushes:04d}'
        event = self.env['cloud.github.event'].create({
            'event_type': 'push',
            'delivery_id': f'delivery-{self.id()}-{self.pushes}',
            'payload': _push_payload(
                self.REPO_URL, self.BRANCH, sha, message or f'commit {sha}',
            ),
        })
        event._process_push_event()
        self.assertTrue(event.processed)
        return self._rebuilds()

    def _rebuilds(self):
        """The instance's rebuild jobs, oldest first."""
        return self.Job.search([
            ('instance_id', '=', self.instance.id),
            ('job_type_id', '=', self.rebuild_type.id),
        ], order='id asc')

    def _running_rebuild(self):
        """A rebuild of the instance created ``started``, as if running."""
        return self.Job.create({
            'name': 'rebuild',
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': self.rebuild_type.id,
            'state': 'started',
        })


class TestPushMerge(_PushCase):

    def test_a_push_with_no_rebuild_starts_one_at_once(self):
        rebuilds = self._push('aaaa111')
        self.assertEqual(len(rebuilds), 1)
        self.assertEqual(rebuilds.state, 'pending')
        self.assertEqual(rebuilds.payload['trigger'], 'webhook')
        self.assertEqual(rebuilds.payload['push_sha'], 'aaaa111')
        self.assertTrue(self.instance.last_auto_rebuild)

    def test_a_push_during_a_rebuild_queues_one_with_the_push_as_head(self):
        running = self._running_rebuild()
        rebuilds = self._push('bbbb222') - running
        self.assertEqual(len(rebuilds), 1)
        self.assertEqual(rebuilds.state, 'queued')
        self.assertEqual(rebuilds.payload['trigger'], 'webhook')
        self.assertEqual(rebuilds.payload['push_sha'], 'bbbb222')

    def test_pushes_during_a_rebuild_merge_into_the_one_waiting(self):
        running = self._running_rebuild()
        self._push('first01', 'first commit')
        self._push('secnd02', 'second commit')
        waiting = self._push('third03', 'third commit') - running
        self.assertEqual(len(waiting), 1, 'one rebuild waits, not three')
        self.assertEqual(waiting.state, 'queued')
        payload = waiting.payload
        self.assertEqual(payload['trigger'], 'coalesced')
        # The head stays the first push; the list carries all three.
        self.assertEqual(payload['push_sha'], 'first01')
        self.assertEqual(payload['push_message'], 'first commit')
        self.assertEqual(
            [p['push_sha'] for p in payload['coalesced_pushes']],
            ['first01', 'secnd02', 'third03'],
        )
        self.assertEqual(
            _webhook_fields(payload)['coalesced_pushes'],
            payload['coalesced_pushes'],
        )
        running.write({'state': 'done'})
        _run_after_commit(self.env)
        self.assertEqual(waiting.state, 'pending')
        self.assertEqual(
            [p['push_sha'] for p in waiting.payload['coalesced_pushes']],
            ['first01', 'secnd02', 'third03'],
        )

    def test_there_is_no_cooldown(self):
        """Two pushes in a row: the second waits behind the first's rebuild."""
        first = self._push('cool001')
        self.assertEqual(first.state, 'pending')
        second = self._push('cool002') - first
        self.assertEqual(len(second), 1)
        self.assertEqual(second.state, 'queued')
        self.assertEqual(second.payload['push_sha'], 'cool002')

    def test_a_rebuild_asked_for_by_hand_is_not_merged_into(self):
        self._running_rebuild()
        manual = self.Job.browse(self.Job.enqueue(
            self.host.id, self.instance.id, 'rebuild_instance',
        ))
        self.assertEqual(manual.state, 'queued')
        pushed = self._push('hand001') - manual
        pushed = pushed.filtered(lambda j: j.state == 'queued')
        self.assertEqual(len(pushed), 1)
        self.assertFalse(manual.payload)

    def test_a_dispatched_rebuild_is_not_merged_into(self):
        first = self._push('disp001')
        self.assertEqual(first.state, 'pending')
        self._push('disp002')
        self.assertEqual(first.payload['trigger'], 'webhook')
        self.assertNotIn('coalesced_pushes', first.payload)

    def test_the_merge_rule_itself(self):
        waiting = self.Job.create({
            'name': 'rebuild',
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': self.rebuild_type.id,
            'state': 'queued',
            'payload': {
                'trigger': 'webhook', 'push_repo': 'owner/repo',
                'push_branch': 'main', 'push_sha': 'one', 'push_message': '1',
                'push_by': 'dev',
            },
        })
        merge = RebuildInstanceExecutor._merge_queued_payload
        self.assertFalse(merge(waiting, {}), 'a manual request is not a push')
        self.assertTrue(merge(waiting, {
            'trigger': 'webhook', 'push_repo': 'owner/repo',
            'push_branch': 'main', 'push_sha': 'two', 'push_message': '2',
            'push_by': 'dev',
        }))
        self.assertEqual(waiting.payload['push_sha'], 'one')
        self.assertEqual(
            [p['push_sha'] for p in waiting.payload['coalesced_pushes']],
            ['one', 'two'],
        )
        waiting.payload = {}
        self.assertFalse(
            merge(waiting, {'trigger': 'webhook', 'push_sha': 'three'}),
            'a rebuild not asked for by a push absorbs no push',
        )

    def test_a_push_to_another_repository_merges_too(self):
        """On purpose: the rebuild pulls every repository's branch head,
        so one build covers the pushes to all of them."""
        waiting = self.Job.create({
            'name': 'rebuild',
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': self.rebuild_type.id,
            'state': 'queued',
            'payload': {
                'trigger': 'webhook', 'push_repo': 'owner/repo',
                'push_branch': 'main', 'push_sha': 'one', 'push_message': '1',
                'push_by': 'dev',
            },
        })
        self.assertTrue(RebuildInstanceExecutor._merge_queued_payload(waiting, {
            'trigger': 'webhook', 'push_repo': 'owner/addons',
            'push_branch': 'main', 'push_sha': 'two', 'push_message': '2',
            'push_by': 'dev',
        }))
        self.assertEqual(waiting.payload['push_repo'], 'owner/repo')
        self.assertEqual(
            [p['push_repo'] for p in waiting.payload['coalesced_pushes']],
            ['owner/repo', 'owner/addons'],
        )
