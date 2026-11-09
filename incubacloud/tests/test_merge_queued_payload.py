"""J2: a push already in the waiting rebuild's list does not grow it.

A delivery repeated while a push's rebuild waits (a GitLab «Resend», a
GitHub redelivery) merges into the same job without adding a second
entry for the same commit. Duplicates compare by
``(push_repo, push_branch, push_sha, push_provider)``; a job queued
before ``push_provider`` existed carries no such key and counts as
GitHub's.
"""
from odoo.tests.common import TransactionCase

from ..models.rebuild_instance_executor import RebuildInstanceExecutor


class TestMergeQueuedPayloadDuplicates(TransactionCase):

    def setUp(self):
        """A host, an instance and a rebuild waiting for a GitHub push."""
        super().setUp()
        host = self.env['cloud.host'].create({
            'name': 'j2-host', 'ip_address': '10.0.0.92', 'user': 'ubuntu',
            'wildcard_domain': 'j2.example.com',
        })
        project = self.env['cloud.project'].create({'name': 'J2'})
        instance = self.env['cloud.instance'].create({
            'name': 'j2-staging', 'project_id': project.id,
            'host_id': host.id, 'environment': 'staging',
        })
        self.waiting = self.env['cloud.job'].create({
            'name': 'rebuild', 'host_id': host.id,
            'instance_id': instance.id,
            'job_type_id': self.env['cloud.job.type'].search(
                [('code', '=', 'rebuild_instance')], limit=1,
            ).id,
            'state': 'queued',
            # Queued before the upgrade: no push_provider key.
            'payload': {
                'trigger': 'webhook', 'push_repo': 'owner/repo',
                'push_branch': 'main', 'push_sha': 'aaa1111',
                'push_message': 'one', 'push_by': 'dev',
            },
        })
        self.merge = RebuildInstanceExecutor._merge_queued_payload

    def _push(self, sha='aaa1111', branch='main', provider='github'):
        """Return the payload of a push of *sha*.

        :rtype: dict
        """
        return {
            'trigger': 'webhook', 'push_repo': 'owner/repo',
            'push_branch': branch, 'push_sha': sha, 'push_message': 'm',
            'push_by': 'dev', 'push_provider': provider,
        }

    def _shas(self):
        """Return the (sha, branch, provider) of every push in the job.

        :rtype: list
        """
        pushes = self.waiting.payload.get('coalesced_pushes') or [
            self.waiting.payload,
        ]
        return [
            (p['push_sha'], p['push_branch'], p.get('push_provider', ''))
            for p in pushes
        ]

    def test_a_repeated_github_push_meets_a_job_without_the_key(self):
        """A job queued before the upgrade is GitHub's: the same push is absorbed, not added."""
        self.assertTrue(self.merge(self.waiting, self._push()))
        self.assertEqual(self._shas(), [('aaa1111', 'main', '')])

    def test_a_repeated_push_does_not_grow_a_merged_list(self):
        """Once merged, the same push again leaves the list as it was."""
        self.assertTrue(self.merge(self.waiting, self._push('bbb2222')))
        self.assertTrue(self.merge(self.waiting, self._push('bbb2222')))
        self.assertEqual(
            [s for s, _b, _p in self._shas()], ['aaa1111', 'bbb2222'],
        )

    def test_the_same_commit_on_another_branch_or_provider_is_kept(self):
        """The key carries the branch and the provider."""
        self.assertTrue(self.merge(self.waiting, self._push(branch='dev')))
        self.assertTrue(self.merge(self.waiting, self._push(provider='gitlab')))
        self.assertEqual(self._shas(), [
            ('aaa1111', 'main', ''), ('aaa1111', 'dev', 'github'),
            ('aaa1111', 'main', 'gitlab'),
        ])

    def test_a_github_push_names_its_provider(self):
        """GitHub's push payload carries push_provider 'github'."""
        self.assertIn('push_provider', RebuildInstanceExecutor._PUSH_KEYS)
