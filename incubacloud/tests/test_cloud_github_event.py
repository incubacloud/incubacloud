"""
Tier 2 — ORM integration tests for cloud.github.event processing.

Tests focus on guard conditions and state transitions, using unittest.mock
to avoid actually enqueueing queue_job tasks.

Covers _process_push_event:
  - deleted push  → skip, mark processed
  - tag push      → skip, mark processed
  - empty URL     → skip, mark processed
  - no repo match → skip, mark processed
  - frozen repo   → no enqueue
  - auto_rebuild=False → no enqueue
  - not deployed  → no enqueue
  - no host_id    → no enqueue
  - cooldown active → no enqueue
  - active job running → no enqueue
  - happy path    → enqueue called, last_auto_rebuild set, processed=True

Covers _process_pull_request_event:
  - missing pr_number  → skip, mark processed
  - missing head_ref   → skip, mark processed
  - empty clone_url    → skip, mark processed
  - unknown action     → mark processed, no side effects
  - closed with no PR instances → mark processed
  - synchronize with no PR instances → mark processed
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_job_type(env, code, apply_to='instance'):
    jt = env['cloud.job.type'].search([('code', '=', code)], limit=1)
    if not jt:
        jt = env['cloud.job.type'].create({
            'name': code,
            'code': code,
            'apply_to': apply_to,
        })
    return jt


def _push_payload(clone_url, branch, deleted=False, sha='abc1234def5678'):
    return json.dumps({
        'ref': f'refs/heads/{branch}',
        'deleted': deleted,
        'after': sha,
        'repository': {
            'clone_url': clone_url,
            'full_name': 'owner/repo',
        },
        'pusher': {'name': 'dev'},
        'head_commit': {'message': 'fix: something\n\nBody'},
    })


def _pr_payload(clone_url, branch, pr_number, action='opened', repo_full='owner/repo'):
    return json.dumps({
        'action': action,
        'pull_request': {
            'number': pr_number,
            'head': {
                'ref': branch,
                'repo': {'clone_url': clone_url},
            },
        },
        'repository': {'full_name': repo_full},
    })


# ── base ──────────────────────────────────────────────────────────────────────

class _GitHubEventBase(TransactionCase):

    REPO_URL = 'https://github.com/owner/repo.git'
    BRANCH = 'main'

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'GH Test Host',
            'ip_address': '10.0.0.2',
            'user': 'ubuntu',
            'wildcard_domain': 'test.example.com',
        })
        self.project = self.env['cloud.project'].create({'name': 'GH Test Project'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'staging-gh',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'auto_rebuild': True,
            'deployed': True,
        })
        self.repo = self.env['cloud.instance.repo'].create({
            'instance_id': self.instance.id,
            'url': self.REPO_URL,
            'branch': self.BRANCH,
        })
        self.Event = self.env['cloud.github.event']

    def _push_event(self, payload):
        return self.Event.create({
            'event_type': 'push',
            'delivery_id': f'delivery-push-{self.id()}',
            'payload': payload,
        })

    def _pr_event(self, payload):
        return self.Event.create({
            'event_type': 'pull_request',
            'delivery_id': f'delivery-pr-{self.id()}',
            'payload': payload,
        })

    def _enqueue_calls(self):
        """Context manager that captures calls to cloud.job.enqueue.

        Returns (calls_list, patcher). Use as::

            calls, mock = self._enqueue_calls()
            with mock:
                ev._process_push_event()
            # calls is populated; each entry is a mock.call object
        """
        from unittest.mock import MagicMock, patch as _patch
        calls = []
        Job = self.env['cloud.job']

        def _side(host_id, instance_id, job_type_code, payload=None):
            calls.append({
                'host_id': host_id,
                'instance_id': instance_id,
                'job_type_code': job_type_code,
                'payload': payload or {},
            })
            return 999

        return calls, _patch.object(type(Job), 'enqueue', side_effect=_side)

    def _force_job_state(self, job, state):
        """Set job state via SQL to bypass ORM write guards.

        ``state`` is a stored field related to ``queue_job_id.state``. On
        Odoo 18 a recompute left pending from ``create`` would overwrite
        the raw value at the next flush (e.g. the flush a ``search`` does
        before querying). Settle the recompute first, then write the
        column and drop the stale cache so the value survives.
        """
        job.flush_recordset()
        self.env.cr.execute(
            "UPDATE cloud_job SET state = %s WHERE id = %s",
            (state, job.id),
        )
        job.invalidate_recordset(['state'])


# ── _process_push_event ───────────────────────────────────────────────────────

class TestProcessPushEventGuards(_GitHubEventBase):

    def test_deleted_push_marks_processed_no_enqueue(self):
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH, deleted=True))
            ev._process_push_event()
        self.assertTrue(ev.processed)
        self.assertEqual(calls, [])

    def test_tag_push_marks_processed_no_enqueue(self):
        payload = json.dumps({
            'ref': 'refs/tags/v1.0.0',
            'deleted': False,
            'after': 'abc1234',
            'repository': {'clone_url': self.REPO_URL, 'full_name': 'owner/repo'},
            'pusher': {'name': 'dev'},
            'head_commit': {'message': 'tag'},
        })
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(payload)
            ev._process_push_event()
        self.assertTrue(ev.processed)
        self.assertEqual(calls, [])

    def test_empty_clone_url_marks_processed_no_enqueue(self):
        payload = json.dumps({
            'ref': 'refs/heads/main',
            'deleted': False,
            'after': 'abc1234',
            'repository': {'clone_url': '', 'full_name': 'owner/repo'},
            'pusher': {'name': 'dev'},
            'head_commit': {'message': 'msg'},
        })
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(payload)
            ev._process_push_event()
        self.assertTrue(ev.processed)
        self.assertEqual(calls, [])

    def test_no_matching_repo_url_no_enqueue(self):
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload('https://github.com/other/repo.git', self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])
        self.assertTrue(ev.processed)

    def test_no_matching_branch_no_enqueue(self):
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, 'feature/other'))
            ev._process_push_event()
        self.assertEqual(calls, [])
        self.assertTrue(ev.processed)

    def test_frozen_repo_no_enqueue(self):
        self.repo.write({'commit_sha': 'f00ba12c0ff33d'})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])

    def test_auto_rebuild_false_no_enqueue(self):
        self.instance.write({'auto_rebuild': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])
        self.assertTrue(ev.processed)

    def test_not_deployed_no_enqueue(self):
        self.instance.write({'deployed': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])

    def test_no_host_no_enqueue(self):
        self.instance.write({'host_id': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])

    def test_cooldown_active_no_enqueue(self):
        self.instance.write({'last_auto_rebuild': fields.Datetime.now()})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])
        self.assertTrue(ev.processed)

    def test_cooldown_expired_allows_enqueue(self):
        past = fields.Datetime.now() - timedelta(minutes=5)
        self.instance.write({'last_auto_rebuild': past})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(len(calls), 1)

    def test_active_job_running_no_enqueue(self):
        rebuild_jt = _ensure_job_type(self.env, 'rebuild_instance')
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': rebuild_jt.id,
            'name': 'Active Rebuild',
        })
        self._force_job_state(job, 'started')
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(
                _push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])

    def test_active_pending_job_no_enqueue(self):
        rebuild_jt = _ensure_job_type(self.env, 'rebuild_instance')
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': rebuild_jt.id,
            'name': 'Pending Rebuild',
        })
        self._force_job_state(job, 'pending')
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(
                _push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])

    def test_done_job_does_not_block(self):
        rebuild_jt = _ensure_job_type(self.env, 'rebuild_instance')
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': rebuild_jt.id,
            'name': 'Done Rebuild',
        })
        self._force_job_state(job, 'done')
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(len(calls), 1)

    # ── Pending push queue (defer-instead-of-drop) ─────────────────────────
    #
    # When a push cannot trigger an immediate rebuild (cooldown active or a
    # rebuild already running) we now persist it into
    # ``cloud.instance.pending.push`` instead of silently dropping it. The
    # ``rebuild_instance_executor.on_success`` hook later coalesces every
    # queued push into a single follow-up rebuild — see
    # test_pending_push_coalesce.

    def test_cooldown_path_records_pending_push(self):
        self.instance.write({'last_auto_rebuild': fields.Datetime.now()})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        # No new rebuild fired …
        self.assertEqual(calls, [])
        # … but the push is queued for the next coalesced rebuild.
        pending = self.instance.pending_push_ids
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending.skip_reason, 'cooldown')
        self.assertEqual(pending.push_branch, self.BRANCH)
        # The body of the payload (sha, message, pusher) is preserved so the
        # UI can render the queued item without a second webhook lookup.
        self.assertTrue(pending.push_sha)
        self.assertEqual(pending.push_by, 'dev')

    def test_active_job_path_records_pending_push(self):
        rebuild_jt = _ensure_job_type(self.env, 'rebuild_instance')
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': rebuild_jt.id,
            'name': 'Active Rebuild',
        })
        self._force_job_state(job, 'started')
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls, [])
        pending = self.instance.pending_push_ids
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending.skip_reason, 'active_job')


class TestProcessPushEventHappyPath(_GitHubEventBase):

    def test_enqueue_called_with_correct_job_type(self):
        self.instance.write({'last_auto_rebuild': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(
                _push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c['host_id'], self.host.id)
        self.assertEqual(c['instance_id'], self.instance.id)
        self.assertEqual(c['job_type_code'], 'rebuild_instance')

    def test_enqueue_payload_contains_trigger_webhook(self):
        self.instance.write({'last_auto_rebuild': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(
                _push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(calls[0]['payload'].get('trigger'), 'webhook')

    def test_last_auto_rebuild_updated(self):
        before = fields.Datetime.now()
        self.instance.write({'last_auto_rebuild': False})
        _, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertIsNotNone(self.instance.last_auto_rebuild)
        self.assertGreaterEqual(self.instance.last_auto_rebuild, before)

    def test_processed_flag_set(self):
        self.instance.write({'last_auto_rebuild': False})
        _, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertTrue(ev.processed)

    def test_head_commit_message_truncated_to_80(self):
        long_msg = 'x' * 120 + '\nBody of commit'
        payload = json.dumps({
            'ref': f'refs/heads/{self.BRANCH}',
            'deleted': False,
            'after': 'abc1234',
            'repository': {'clone_url': self.REPO_URL, 'full_name': 'owner/repo'},
            'pusher': {'name': 'dev'},
            'head_commit': {'message': long_msg},
        })
        self.instance.write({'last_auto_rebuild': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(payload)
            ev._process_push_event()
        push_msg = calls[0]['payload']['push_message']
        self.assertLessEqual(len(push_msg), 80)

    def test_multiple_instances_same_repo_all_enqueued(self):
        """Two instances watching the same repo both get triggered."""
        instance2 = self.env['cloud.instance'].create({
            'name': 'staging-gh-2',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'auto_rebuild': True,
            'deployed': True,
        })
        self.env['cloud.instance.repo'].create({
            'instance_id': instance2.id,
            'url': self.REPO_URL,
            'branch': self.BRANCH,
        })
        self.instance.write({'last_auto_rebuild': False})
        instance2.write({'last_auto_rebuild': False})
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._push_event(_push_payload(self.REPO_URL, self.BRANCH))
            ev._process_push_event()
        self.assertEqual(len(calls), 2)


# ── _process_pull_request_event ───────────────────────────────────────────────

class TestProcessPREventGuards(_GitHubEventBase):

    def test_missing_pr_number_marks_processed(self):
        payload = json.dumps({
            'action': 'opened',
            'pull_request': {
                'number': None,
                'head': {'ref': 'feat/branch', 'repo': {'clone_url': self.REPO_URL}},
            },
            'repository': {'full_name': 'owner/repo'},
        })
        ev = self._pr_event(payload)
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_missing_head_ref_marks_processed(self):
        payload = json.dumps({
            'action': 'opened',
            'pull_request': {
                'number': 42,
                'head': {'ref': '', 'repo': {'clone_url': self.REPO_URL}},
            },
            'repository': {'full_name': 'owner/repo'},
        })
        ev = self._pr_event(payload)
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_empty_clone_url_marks_processed(self):
        payload = json.dumps({
            'action': 'opened',
            'pull_request': {
                'number': 42,
                'head': {'ref': 'feat/x', 'repo': {'clone_url': ''}},
            },
            'repository': {'full_name': 'owner/repo'},
        })
        ev = self._pr_event(payload)
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_unknown_action_marks_processed_no_side_effects(self):
        ev = self._pr_event(_pr_payload(self.REPO_URL, 'feat/x', 99, action='labeled'))
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_closed_with_no_pr_instances_marks_processed(self):
        ev = self._pr_event(_pr_payload(self.REPO_URL, 'feat/x', 999, action='closed'))
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_synchronize_with_no_pr_instances_marks_processed(self):
        ev = self._pr_event(_pr_payload(self.REPO_URL, 'feat/x', 999, action='synchronize'))
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_opened_no_matching_prod_repos_marks_processed(self):
        # No production instance with this repo → nothing to clone
        ev = self._pr_event(_pr_payload(self.REPO_URL, 'feat/x', 42, action='opened'))
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_empty_payload_marks_processed(self):
        ev = self.Event.create({
            'event_type': 'pull_request',
            'delivery_id': 'empty-pr',
            'payload': '{}',
        })
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)

    def test_synchronize_updates_branch_on_matching_repo(self):
        """synchronize action updates branch on matching PR instance repos."""
        pr_instance = self.env['cloud.instance'].create({
            'name': 'pr-7',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'deployed': True,
            'pr_number': 7,
            'pr_repo': 'owner/repo',
        })
        pr_repo = self.env['cloud.instance.repo'].create({
            'instance_id': pr_instance.id,
            'url': self.REPO_URL,
            'branch': 'old-branch',
        })
        new_head = 'feat/updated'
        payload = json.dumps({
            'action': 'synchronize',
            'pull_request': {
                'number': 7,
                'head': {
                    'ref': new_head,
                    'repo': {'clone_url': self.REPO_URL},
                },
            },
            'repository': {'full_name': 'owner/repo'},
        })
        calls, mock = self._enqueue_calls()
        with mock:
            ev = self._pr_event(payload)
            ev._process_pull_request_event()
        self.assertTrue(ev.processed)
        self.assertEqual(pr_repo.branch, new_head)
        self.assertFalse(pr_repo.commit_sha)

    def test_closed_undeployed_pr_instance_unlinked(self):
        """Closing a PR on an un-deployed instance directly unlinks it."""
        pr_instance = self.env['cloud.instance'].create({
            'name': 'pr-closed',
            'project_id': self.project.id,
            'environment': 'staging',
            'deployed': False,
            'pr_number': 55,
            'pr_repo': 'owner/repo',
        })
        pr_id = pr_instance.id
        payload = json.dumps({
            'action': 'closed',
            'pull_request': {
                'number': 55,
                'head': {
                    'ref': 'feat/pr',
                    'repo': {'clone_url': self.REPO_URL},
                },
            },
            'repository': {'full_name': 'owner/repo'},
        })
        ev = self._pr_event(payload)
        ev._process_pull_request_event()
        self.assertTrue(ev.processed)
        self.assertFalse(
            self.env['cloud.instance'].browse(pr_id).exists(),
            "Un-deployed PR instance should be unlinked on PR close",
        )


class TestDeliveryIdAntiReplay(TransactionCase):
    """Regression guard for the partial unique index on delivery_id.

    GitHub's X-GitHub-Delivery UUID is unique per delivery by contract.
    A duplicate insert means either a retry after a transient failure
    or a captured request being replayed. The DB constraint lets the
    controller detect and swallow the duplicate without re-firing the
    push / pull_request handlers.
    """

    def test_duplicate_delivery_id_raises_integrity_error(self):
        from psycopg2 import errors as pg_errors

        Event = self.env['cloud.github.event'].sudo()
        Event.create({
            'event_type': 'push', 'action': '',
            'delivery_id': 'dup-uuid-1', 'payload': '{}',
        })
        # Second insert with the same delivery_id must raise at the DB
        # layer. Using a savepoint so the outer test transaction stays
        # usable for the assertion afterwards.
        with self.assertRaises(pg_errors.UniqueViolation):
            with self.env.cr.savepoint():
                Event.create({
                    'event_type': 'push', 'action': '',
                    'delivery_id': 'dup-uuid-1', 'payload': '{}',
                })

    def test_empty_delivery_id_allows_multiple_rows(self):
        """Partial index WHERE delivery_id != '' — legacy/test rows with
        empty delivery_id must coexist so the install-time index
        creation doesn't fail on historic data."""
        Event = self.env['cloud.github.event'].sudo()
        Event.create({
            'event_type': 'ping', 'action': '',
            'delivery_id': '', 'payload': '{}',
        })
        # Should not raise — partial index exempts empty delivery_id.
        Event.create({
            'event_type': 'ping', 'action': '',
            'delivery_id': '', 'payload': '{}',
        })


class TestPurgeCloudGitHubEvent(TransactionCase):
    """Retention cron behaviour for ``cloud.github.event``.

    Two independent stages are covered: payload truncation for mid-age
    rows, outright deletion for very old rows, and preservation of
    fresh rows and unprocessed rows regardless of age.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env['cloud.settings'].sudo()._get()
        self.settings.write({
            'github_event_truncate_days': 7,
            'github_event_retention_days': 30,
        })
        self.Event = self.env['cloud.github.event'].sudo()

    def _make(self, days_old, processed=True, payload=None, delivery=None):
        payload = payload or json.dumps({
            'ref': 'refs/heads/main',
            'after': 'a' * 40,
            'pusher': {'name': 'alice'},
            'repository': {'full_name': 'owner/repo'},
            'extra': 'x' * 1000,
        })
        ev = self.Event.create({
            'event_type': 'push',
            'action': '',
            'delivery_id': delivery or f'purge-{days_old}-{fields.Datetime.now()}',
            'payload': payload,
            'processed': processed,
        })
        backdate = fields.Datetime.now() - timedelta(days=days_old)
        self.env.cr.execute(
            "UPDATE cloud_github_event SET create_date = %s WHERE id = %s",
            (backdate, ev.id),
        )
        ev.invalidate_recordset(['create_date'])
        return ev

    def test_old_processed_event_is_deleted(self):
        ev = self._make(days_old=45, processed=True)
        self.Event._purge_old()
        self.assertFalse(ev.exists(), "row >retention_days must be unlinked")

    def test_unprocessed_event_is_kept(self):
        ev = self._make(days_old=120, processed=False)
        self.Event._purge_old()
        self.assertTrue(
            ev.exists(),
            "unprocessed rows must survive purge regardless of age",
        )

    def test_mid_age_payload_is_truncated(self):
        ev = self._make(days_old=10, processed=True)
        original_len = len(ev.payload)
        self.Event._purge_old()
        self.assertTrue(ev.exists(), "row below retention must stay")
        data = json.loads(ev.payload)
        self.assertTrue(
            data.get('_truncated'),
            "payload should be replaced by a stub marked _truncated",
        )
        self.assertEqual(data.get('_original_size'), original_len)
        self.assertEqual(data.get('ref'), 'refs/heads/main')
        self.assertLess(len(ev.payload), original_len)

    def test_fresh_event_is_untouched(self):
        ev = self._make(days_old=1, processed=True)
        snapshot = ev.payload
        self.Event._purge_old()
        self.assertEqual(
            ev.payload, snapshot,
            "events younger than truncate_days must keep full payload",
        )
