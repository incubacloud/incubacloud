"""Pull request previews: who gets one, who decides, and what a failure says.

A pull request on a repository a production instance follows creates a
staging carrying a copy of that instance's data. These tests pin the
four things that make the feature governable:

* a hook that lets a layer on top keep its own instances out;
* a project switch that starts off and travels through the API;
* a failure that is reported where people look — the pull request and
  the production instance — instead of a log line;
* the preview's address, which the success comment promises.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.github.client import GitHubAPIError, GitHubAppClient
from odoo.addons.incubacloud.models import cloud_github_event, cloud_instance
from odoo.addons.incubacloud.models.cloud_github_event import (
    PR_PREVIEW_FAILED_CODE,
)
from odoo.addons.incubacloud.models.cloud_gitlab_mr_note import (
    PR_PREVIEW_NOTE_FAILED_CODE,
)
from odoo.addons.queue_job.exception import RetryableJobError

REPO_URL = 'https://github.com/owner/repo.git'
#: ``cloud_github_event.PR_OTHER_INSTALLATION_CODE``.
PR_OTHER_INSTALLATION_CODE = 'github_pr_other_installation'
REPO_FULL = 'owner/repo'
FORK_URL = 'https://github.com/someone/repo.git'
FORK_FULL = 'someone/repo'
INSTALLATION_ID = 4242


def _pr_payload(
    pr_number, action='opened', branch='feature-x', *,
    installation=INSTALLATION_ID, base=(REPO_FULL, REPO_URL),
    head=(REPO_FULL, REPO_URL),
):
    """Return the JSON a ``pull_request`` delivery carries.

    :param int installation: the App installation GitHub sent it for
    :param tuple base: ``(full_name, clone_url)`` of the repository the
        pull request is opened on, where the installation lives
    :param tuple head: ``(full_name, clone_url)`` of the repository its
        branch comes from; a fork when it differs from *base*
    """
    return json.dumps({
        'action': action,
        'installation': {'id': installation},
        'pull_request': {
            'number': pr_number,
            'base': {'ref': 'main', 'repo': {
                'full_name': base[0], 'clone_url': base[1],
            }},
            'head': {'ref': branch, 'repo': head and {
                'full_name': head[0], 'clone_url': head[1],
            }},
        },
        'repository': {'full_name': base[0], 'clone_url': base[1]},
    })


class _PreviewBase(TransactionCase):
    """A deployed production instance following an unpinned repository."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'PR Preview Host',
            'ip_address': '10.0.0.9',
            'user': 'ubuntu',
            'wildcard_domain': '*.preview.example.com',
        })
        self.project = self.env['cloud.project'].create({
            'name': 'PR Preview Project',
            'pr_reviews_enabled': True,
        })
        self.prod = self.env['cloud.instance'].create({
            'name': 'prod',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'production',
            'state': 'deployed',
        })
        # The production's own line is what it follows; the project, with
        # no lines, is only the template new instances start from. The
        # App's installation is the panel's own.
        self.prod_line = self.env['cloud.instance.repo'].create({
            'instance_id': self.prod.id,
            'url': REPO_URL,
            'branch': 'main',
        })
        App = self.env['cloud.github.app']
        App.search([]).unlink()
        App.create({
            'app_id': '12345',
            'private_key': 'not a key',
            'installation_id': str(INSTALLATION_ID),
        })

    def _event(self, pr_number=7, action='opened', **payload):
        """Create an unprocessed ``pull_request`` event.

        :param payload: passed on to ``_pr_payload``
        """
        return self.env['cloud.github.event'].create({
            'event_type': 'pull_request',
            'delivery_id': f'pr-preview-{self.id()}-{pr_number}-{action}',
            'payload': _pr_payload(pr_number, action=action, **payload),
        })

    def _clone_patch(self, **kwargs):
        """Patch ``clone_to_staging`` on the model class."""
        return patch.object(
            type(self.env['cloud.instance']), 'clone_to_staging', **kwargs,
        )

    def _github_patches(self):
        """Stand in for the GitHub App: return (client mock, patchers)."""
        client = MagicMock(spec=GitHubAppClient)
        Service = type(self.env['cloud.github.credential.service'])
        return client, (
            patch.object(
                cloud_github_event, 'GitHubAppClient', return_value=client,
            ),
            patch.object(Service, 'get_credentials', return_value=object()),
        )

    def _alert(self):
        """Return the active failure alert on the production instance."""
        return self.env['cloud.alert'].search([
            ('code', '=', PR_PREVIEW_FAILED_CODE),
            ('instance_id', '=', self.prod.id),
            ('state', '=', 'active'),
        ])


class TestPreviewEligibility(_PreviewBase):

    def test_an_eligible_instance_is_cloned(self):
        with self._clone_patch(return_value={}) as clone:
            self._event()._process_pull_request_event()
        clone.assert_called_once()
        self.assertEqual(clone.call_args.args, ('pr-7',))
        self.assertEqual(clone.call_args.kwargs['pr_number'], 7)
        self.assertEqual(clone.call_args.kwargs['pr_repo'], REPO_FULL)
        self.assertEqual(clone.call_args.kwargs['pr_head_branch'], 'feature-x')

    def test_the_hook_can_decline_an_instance(self):
        Event = type(self.env['cloud.github.event'])
        with self._clone_patch(return_value={}) as clone, patch.object(
            Event, '_pr_preview_allowed', return_value=False,
        ) as hook:
            event = self._event()
            event._process_pull_request_event()
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[0], self.prod)
        clone.assert_not_called()
        self.assertTrue(event.processed)
        self.assertFalse(self._alert(), "declining is not a failure")

    def test_the_default_hook_says_yes(self):
        self.assertTrue(
            self.env['cloud.github.event']._pr_preview_allowed(self.prod),
        )

    def test_a_project_switched_off_gets_nothing(self):
        self.project.pr_reviews_enabled = False
        with self._clone_patch(return_value={}) as clone:
            self._event()._process_pull_request_event()
        clone.assert_not_called()


class TestPreviewBinding(_PreviewBase):
    """Only the panel's own installation, on a repository a production's
    own lines follow, and never from a fork: the App is public, so a valid
    signature says GitHub sent the event, not that it is about one of ours."""

    def _assert_nothing(self, **payload):
        """Run an ``opened`` event built with *payload*: nothing is cloned
        and the event is still marked processed."""
        with self._clone_patch(return_value={}) as clone:
            event = self._event(**payload)
            event._process_pull_request_event()
        clone.assert_not_called()
        self.assertTrue(event.processed)
        self.assertFalse(self._alert(), "ignoring is not a failure")

    def test_another_installation_gets_nothing(self):
        self._assert_nothing(installation=INSTALLATION_ID + 1)

    def test_an_event_without_installation_gets_nothing(self):
        self._assert_nothing(installation=None)

    def test_nothing_while_the_panel_has_no_installation(self):
        self.env['cloud.github.app'].search([]).installation_id = False
        self._assert_nothing()

    def test_a_repository_only_the_production_follows_gets_a_preview(self):
        """The project's lines are a template: an instance often follows
        repositories its project does not list."""
        self.assertFalse(self.project.repo_ids)
        with self._clone_patch(return_value={}) as clone:
            self._event()._process_pull_request_event()
        clone.assert_called_once()

    def test_a_repository_of_the_project_the_production_does_not_follow_gets_nothing(self):
        self.env['cloud.project.repo'].with_context(
            skip_apply_requirements=True,
        ).create({
            'project_id': self.project.id,
            'url': REPO_URL,
            'branch': 'main',
        })
        self.prod_line.with_context(skip_apply_requirements=True).url = (
            'https://github.com/owner/other.git'
        )
        self._assert_nothing()

    def test_a_production_line_saved_in_another_spelling_is_the_same_repository(self):
        """A line saved before its URL was stored canonical still matches."""
        self.env.cr.execute(
            "UPDATE cloud_instance_repo SET url = %s WHERE id = %s",
            ('git@github.com:Owner/Repo.git', self.prod_line.id),
        )
        self.prod_line.invalidate_recordset(['url'])
        with self._clone_patch(return_value={}) as clone:
            self._event()._process_pull_request_event()
        clone.assert_called_once()

    def test_a_pull_request_from_a_fork_gets_nothing(self):
        self._assert_nothing(head=(FORK_FULL, FORK_URL))

    def test_a_fork_whose_head_is_a_followed_repository_gets_nothing(self):
        """Base on a stranger's repository, head on the one production
        follows: matching on the head used to clone production."""
        self._assert_nothing(
            base=(FORK_FULL, FORK_URL), head=(REPO_FULL, REPO_URL),
        )

    def test_a_deleted_fork_gets_nothing(self):
        self._assert_nothing(head=None)

    def test_a_closed_pull_request_of_another_installation_keeps_the_preview(self):
        preview = self.env['cloud.instance'].create({
            'name': 'pr-7',
            'project_id': self.project.id,
            'environment': 'staging',
            'pr_number': 7,
            'pr_repo': REPO_FULL,
        })
        event = self._event(
            action='closed', installation=INSTALLATION_ID + 1,
        )
        event._process_pull_request_event()
        self.assertTrue(preview.exists())
        self.assertTrue(event.processed)


class TestPreviewSwitch(_PreviewBase):

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _request(self):
        """Return a request stand-in bound to the test environment."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        return fake

    def test_a_new_project_starts_switched_off(self):
        project = self.env['cloud.project'].create({'name': 'Fresh Project'})
        self.assertFalse(project.pr_reviews_enabled)

    def test_the_switch_is_read_through_the_api(self):
        with patch.object(_routes_crud, 'request', self._request()):
            data = self.controller.cloud_get_project(self.project.id)
        self.assertIs(data['pr_reviews_enabled'], True)

    def test_the_switch_is_saved_through_the_api(self):
        with patch.object(_routes_crud, 'request', self._request()):
            self.controller.cloud_save_project(
                self.project.id, {'pr_reviews_enabled': False},
            )
        self.assertFalse(self.project.pr_reviews_enabled)

    def test_a_project_can_be_created_with_it_on(self):
        with patch.object(_routes_crud, 'request', self._request()):
            result = self.controller.cloud_create_project({
                'name': 'Opted In', 'pr_reviews_enabled': True,
            })
        project = self.env['cloud.project'].browse(result['id'])
        self.assertTrue(project.pr_reviews_enabled)


class TestPreviewFailureIsVisible(_PreviewBase):

    def _process(self, clone_side_effect):
        """Run one ``opened`` event; return (event, GitHub client mock)."""
        client, patches = self._github_patches()
        with self._clone_patch(side_effect=clone_side_effect), \
                patches[0], patches[1]:
            event = self._event()
            event._process_pull_request_event()
        return event, client

    def test_a_refusal_is_commented_with_its_reason(self):
        event, client = self._process(UserError("No room\non this host."))
        client.post_issue_comment.assert_called_once()
        owner, repo, number, body = client.post_issue_comment.call_args.args
        self.assertEqual((owner, repo, number), ('owner', 'repo', 7))
        # Collapsed to one line: a newline would break the Markdown table.
        self.assertIn('No room on this host.', body)
        self.assertIn('could not be created', body)
        self.assertTrue(event.processed)

    def test_a_refusal_raises_an_alert_on_production(self):
        self._process(UserError("No room on this host."))
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'warning')
        self.assertIn('No room on this host.', alert.message)
        self.assertIn('#7', alert.message)

    def test_an_unexpected_error_never_reaches_the_pull_request(self):
        _event, client = self._process(
            RuntimeError("psql: password 'hunter2' rejected"),
        )
        body = client.post_issue_comment.call_args.args[3]
        self.assertNotIn('hunter2', body)
        self.assertNotIn('psql', body)
        self.assertIn('ref:', body)
        self.assertNotIn('hunter2', self._alert().message)

    def test_a_later_success_closes_the_alert(self):
        self._process(UserError("No room on this host."))
        self.assertTrue(self._alert())
        with self._clone_patch(return_value={}):
            self._event(action='reopened')._process_pull_request_event()
        self.assertFalse(self._alert())

    def test_failing_to_comment_does_not_fail_the_event(self):
        client, patches = self._github_patches()
        client.post_issue_comment.side_effect = RuntimeError("GitHub is down")
        with self._clone_patch(side_effect=UserError("No room.")), \
                patches[0], patches[1]:
            event = self._event()
            event._process_pull_request_event()
        self.assertTrue(event.processed)
        self.assertTrue(self._alert(), "the alert does not depend on GitHub")

    def test_a_clone_that_fails_half_way_leaves_nothing_behind(self):
        Instance = self.env['cloud.instance']

        def _half_way(prod, name, **kwargs):
            Instance.create({
                'name': name,
                'project_id': prod.project_id.id,
                'host_id': prod.host_id.id,
                'environment': 'staging',
                'pr_number': kwargs['pr_number'],
                'pr_repo': kwargs['pr_repo'],
            })
            raise UserError("No room on this host.")

        client, patches = self._github_patches()
        with patch.object(
            type(Instance), 'clone_to_staging', autospec=True,
            side_effect=_half_way,
        ), patches[0], patches[1]:
            self._event()._process_pull_request_event()
        self.assertFalse(
            Instance.search([('pr_number', '=', 7), ('pr_repo', '=', REPO_FULL)]),
            "a leftover row would make every reopen find the preview "
            "as already existing",
        )
        self.assertTrue(self._alert())


class TestPreviewAddress(_PreviewBase):

    def _clone(self):
        """Clone the production instance as PR 7 without enqueuing jobs."""
        with patch.object(
            type(self.env['cloud.job']), 'enqueue_chain', return_value=[1, 2, 3],
        ):
            result = self.prod.clone_to_staging(
                'pr-7', pr_number=7, pr_repo=REPO_FULL,
                pr_head_branch='feature-x',
            )
        return self.env['cloud.instance'].browse(result['staging_id'])

    def test_a_preview_gets_an_address_under_the_host_wildcard(self):
        staging = self._clone()
        self.assertEqual(
            staging.domain,
            f'{self.project.remote_folder}-pr-7.preview.example.com',
        )

    def test_the_ready_comment_links_to_it(self):
        staging = self._clone()
        body = staging._pr_preview_ready_body()
        self.assertIn(f'https://{staging.domain}', body)
        self.assertIn('`feature-x`', body)
        self.assertNotIn('no domain configured', body)

    def test_the_preview_follows_the_pull_request_branch(self):
        staging = self._clone()
        repo = staging.repo_ids.filtered(lambda r: r.url == REPO_URL)
        self.assertEqual(repo.branch, 'feature-x')
        self.assertFalse(repo.commit_sha)


class TestPreviewCommentId(_PreviewBase):
    """The preview keeps the id of the comment it posts, to edit and delete
    it later. GitHub's comment ids are past 2^31: storing one failed with
    "integer out of range" and the preview ended in error (11-oct-2026)."""

    BIG_ID = 3_456_789_012

    def _preview(self):
        """Clone the production instance as PR 7 without enqueuing jobs."""
        with patch.object(
            type(self.env['cloud.job']), 'enqueue_chain', return_value=[1, 2, 3],
        ):
            result = self.prod.clone_to_staging(
                'pr-7', pr_number=7, pr_repo=REPO_FULL,
                pr_head_branch='feature-x',
            )
        return self.env['cloud.instance'].browse(result['staging_id'])

    def _comment_patches(self):
        """Stand in for the GitHub App the preview comments with: return
        (client mock, patchers); a new comment gets ``BIG_ID``."""
        client = MagicMock(spec=GitHubAppClient)
        client.post_issue_comment.return_value = {'id': self.BIG_ID}
        Service = type(self.env['cloud.github.credential.service'])
        return client, (
            patch.object(cloud_instance, 'GitHubAppClient', return_value=client),
            patch.object(Service, 'get_credentials', return_value=object()),
        )

    def test_a_comment_id_past_32_bits_is_kept_and_edited(self):
        staging = self._preview()
        client, patches = self._comment_patches()
        with patches[0], patches[1]:
            staging._post_or_update_pr_comment('ready')
            self.env.flush_all()
            staging.invalidate_recordset(['pr_comment_id'])
            self.assertEqual(staging.pr_comment_id, self.BIG_ID)
            staging._post_or_update_pr_comment('ready again')
        client.patch_issue_comment.assert_called_once_with(
            'owner', 'repo', self.BIG_ID, 'ready again',
        )

    def test_the_column_is_a_bigint(self):
        self.env.cr.execute("""
            SELECT udt_name FROM information_schema.columns
             WHERE table_name = 'cloud_instance'
               AND column_name = 'pr_comment_id'
        """)
        self.assertEqual(self.env.cr.fetchone()[0], 'int8')


class TestPreviewCommentInstallation(_PreviewBase):
    """The comment is the installation's that sent the pull request, and
    a comment GitHub does not take is seen on the production.

    The App may be installed on more than one account; the panel keeps
    one installation. A preview's comment was written, and deleted, with
    that one's token whatever installation sent the request: GitHub
    refused it, the refusal went to the server's log alone, and the
    restore ended done with the pull request left without its comment.
    """

    OTHER_INSTALLATION = '9999'

    def _preview(self, installation=OTHER_INSTALLATION, **vals):
        """Create the preview of PR 7, deployed, as its clone through
        *installation* leaves it."""
        return self.env['cloud.instance'].sudo().create({
            'name': 'pr-7', 'project_id': self.project.id,
            'host_id': self.host.id, 'environment': 'staging',
            'state': 'deployed', 'pr_number': 7, 'pr_repo': REPO_FULL,
            'pr_head_branch': 'feature-x', 'pr_state': 'open',
            'pr_installation_id': installation,
        } | vals)

    def _client(self):
        """Stand in for the client of the App: return (client, class patch)."""
        client = MagicMock(spec=GitHubAppClient)
        client.post_issue_comment.return_value = {'id': 31}
        client.get.return_value = []
        return client, patch.object(
            cloud_instance, 'GitHubAppClient', return_value=client,
        )

    def _note_alert(self, instance):
        """Return the active comment alert on *instance*."""
        return self.env['cloud.alert'].search([
            ('code', '=', PR_PREVIEW_NOTE_FAILED_CODE),
            ('instance_id', '=', instance.id),
            ('state', '=', 'active'),
        ])

    def test_the_event_s_installation_is_kept_on_the_preview(self):
        with self._clone_patch(return_value={}) as clone:
            self._event()._process_pull_request_event()
        self.assertEqual(
            clone.call_args.kwargs['pr_installation_id'], str(INSTALLATION_ID),
        )

    def test_the_comment_is_written_with_the_event_s_installation(self):
        preview = self._preview()
        client, patcher = self._client()
        with patcher as cls:
            preview._job_pr_comment()
        creds = cls.call_args.args[0]
        self.assertEqual(creds.installation_id, self.OTHER_INSTALLATION)
        self.assertEqual(creds.app_id, '12345')
        client.post_issue_comment.assert_called_once()
        self.assertEqual(preview.pr_comment_id, 31)

    def test_the_comment_is_deleted_with_it_too(self):
        preview = self._preview(pr_comment_id=31)
        client, patcher = self._client()
        with patcher as cls:
            preview._delete_pr_comment()
        self.assertEqual(cls.call_args.args[0].installation_id, self.OTHER_INSTALLATION)
        client.delete_issue_comment.assert_called_once_with('owner', 'repo', 31)

    def test_a_preview_without_one_uses_the_panel_s(self):
        preview = self._preview(installation='')
        _client, patcher = self._client()
        with patcher as cls:
            preview._job_pr_comment()
        self.assertEqual(cls.call_args.args[0].installation_id, str(INSTALLATION_ID))

    def test_a_refused_comment_raises_an_alert_on_production(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = GitHubAPIError(
            403, 'Resource not accessible by integration',
        )
        with patcher:
            preview._job_pr_comment()
        alert = self._note_alert(self.prod)
        self.assertEqual(len(alert), 1)
        self.assertIn('#7', alert.message)
        self.assertIn('403', alert.message)

    def test_github_away_is_tried_again_before_the_alert(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = GitHubAPIError(502, 'Bad gateway')
        Note = type(self.env['cloud.gitlab.mr.note'])
        with patcher, patch.object(Note, '_last_attempt', return_value=False), \
                self.assertRaises(RetryableJobError):
            preview._job_pr_comment()
        self.assertFalse(self._note_alert(self.prod))
        with patcher:
            preview._job_pr_comment()
        self.assertTrue(self._note_alert(self.prod), "the last try alerts")

    def test_a_comment_that_goes_through_resolves_the_alert(self):
        preview = self._preview()
        self.env['cloud.alert'].sudo().raise_alert(
            PR_PREVIEW_NOTE_FAILED_CODE, 'x', level='warning', instance=self.prod,
            key=f'github_pr:{REPO_FULL}:7',
        )
        _client, patcher = self._client()
        with patcher:
            preview._job_pr_comment()
        self.assertFalse(self._note_alert(self.prod))

    def test_a_closed_or_undeployed_preview_gets_no_comment(self):
        for name, vals in (('pr-7-a', {'pr_state': 'closed'}),
                           ('pr-7-b', {'state': 'deploying'})):
            with self.subTest(**vals):
                preview = self._preview(name=name, **vals)
                client, patcher = self._client()
                with patcher:
                    preview._job_pr_comment()
                client.post_issue_comment.assert_not_called()

    def test_queued_twice_it_is_one_job(self):
        preview = self._preview()
        preview._enqueue_pr_comment()
        preview._enqueue_pr_comment()
        jobs = self.env['queue.job'].sudo().search([
            ('model_name', '=', 'cloud.instance'),
            ('method_name', '=', '_job_pr_comment'),
        ])
        self.assertEqual(len(jobs), 1)


class _CommentCase(_PreviewBase):
    """What the comment of a pull request preview says, and how it is found.

    The preview and the client of the App of
    ``TestPreviewCommentInstallation``, without its tests.
    """

    OTHER_INSTALLATION = TestPreviewCommentInstallation.OTHER_INSTALLATION
    _preview = TestPreviewCommentInstallation._preview
    _client = TestPreviewCommentInstallation._client
    _note_alert = TestPreviewCommentInstallation._note_alert

    def _comment_jobs(self):
        """Return the queued jobs of the pull request's comment."""
        return self.env['queue.job'].sudo().search([
            ('model_name', '=', 'cloud.instance'),
            ('method_name', '=', '_job_pr_comment'),
        ])


class TestPreviewUpdateFailedComment(_CommentCase):
    """A preview whose update failed says so on its pull request.

    Its database was restored, but updating its modules to the request's
    code failed: Odoo was left stopped, the restore failed, and nobody on
    the pull request heard of it — the «ready» comment is queued by the
    restore's success alone. The restore's failure now queues the comment,
    which says it could not be updated and why; a rebuild that succeeds
    makes it say «ready».
    """

    REASON = 'Updating its modules to this code failed (exit status 1).'

    def _restore_fails_in_its_update(self, preview):
        """Run a restore's ``on_failure`` on *preview*, its update failed."""
        from odoo.addons.incubacloud.models.restore_instance_executor import (
            RestoreInstanceExecutor,
        )
        ex = object.__new__(RestoreInstanceExecutor)
        ex.job = SimpleNamespace(id=1, instance_id=preview, payload={})
        ex.env = self.env
        lines = []
        with patch.object(
            RestoreInstanceExecutor, '_sys', autospec=True,
            side_effect=lambda _self, line: lines.append(line),
        ):
            asyncio.run(ex.on_failure(
                {'Restore database': {'exit_status': 0, 'stdout': ''},
                 'Update changed modules': {'exit_status': 1, 'stdout': ''}},
                ["'Update changed modules' exited with status 1"],
            ))
        return lines

    def test_the_failure_is_kept_and_its_comment_queued(self):
        preview = self._preview()
        lines = self._restore_fails_in_its_update(preview)
        self.assertEqual(preview.status, 'error')
        self.assertIn('exit status 1', preview.pr_update_error)
        self.assertEqual(len(self._comment_jobs()), 1)
        self.assertTrue(any('Pushing a fix' in line for line in lines))

    def test_another_failure_queues_nothing(self):
        preview = self._preview()
        from odoo.addons.incubacloud.models.restore_instance_executor import (
            RestoreInstanceExecutor,
        )
        ex = object.__new__(RestoreInstanceExecutor)
        ex.job = SimpleNamespace(id=1, instance_id=preview, payload={})
        ex.env = self.env
        with patch.object(RestoreInstanceExecutor, '_sys', autospec=True):
            asyncio.run(ex.on_failure(
                {'Restore database': {'exit_status': 1, 'stdout': ''}},
                ["'Restore database' exited with status 1"],
            ))
        self.assertFalse(preview.pr_update_error)
        self.assertFalse(self._comment_jobs())

    def test_the_comment_says_it_could_not_be_updated_and_why(self):
        preview = self._preview(pr_update_error=self.REASON)
        client, patcher = self._client()
        with patcher:
            preview._job_pr_comment()
        body = client.post_issue_comment.call_args.args[3]
        self.assertIn('could not be updated', body)
        self.assertIn(self.REASON, body)
        self.assertIn('Push a fix', body)
        self.assertNotIn('ready', body)

    def test_a_rebuild_that_succeeds_resolves_and_says_ready(self):
        preview = self._preview(pr_update_error=self.REASON, pr_comment_id=31)
        preview._preview_rebuilt(self.env['cloud.job'])
        self.assertFalse(preview.pr_update_error)
        self.assertEqual(len(self._comment_jobs()), 1)
        client, patcher = self._client()
        with patcher:
            preview._job_pr_comment()
        body = client.patch_issue_comment.call_args.args[3]
        self.assertIn('ready', body)

    def test_a_rebuild_of_a_preview_without_comment_writes_it(self):
        preview = self._preview()
        preview._preview_rebuilt(self.env['cloud.job'])
        self.assertEqual(len(self._comment_jobs()), 1)

    def test_a_rebuild_of_a_preview_with_its_comment_writes_nothing(self):
        preview = self._preview(pr_comment_id=31)
        preview._preview_rebuilt(self.env['cloud.job'])
        self.assertFalse(self._comment_jobs())

    def test_a_closed_request_hears_nothing(self):
        preview = self._preview(pr_state='closed')
        preview._preview_rebuilt(self.env['cloud.job'])
        self.assertFalse(self._comment_jobs())


class TestPreviewCommentFoundAgain(_CommentCase):
    """A comment written by a try whose answer never came is not written twice.

    The job writes the comment, GitHub takes it, the answer is lost on the
    way and the job runs again: with no id kept, it wrote a second one. The
    comment carries a hidden marker now, and is looked for by it.
    """

    def test_the_comment_carries_the_marker(self):
        preview = self._preview()
        self.assertIn(
            preview._pr_note_marker(), preview._pr_preview_ready_body(),
        )
        self.assertTrue(preview._pr_note_marker().startswith('<!-- incubacloud:preview:'))

    def test_a_comment_found_by_its_marker_is_rewritten(self):
        preview = self._preview()
        client, patcher = self._client()
        client.get.return_value = [
            {'id': 5, 'body': preview._pr_note_marker(),
             'performed_via_github_app': {'id': 999}},
            {'id': 6, 'body': 'thanks ' + preview._pr_note_marker(),
             'performed_via_github_app': {'id': 12345}},
        ]
        with patcher:
            preview._job_pr_comment()
        client.post_issue_comment.assert_not_called()
        self.assertEqual(client.patch_issue_comment.call_args.args[2], 6)
        self.assertEqual(preview.pr_comment_id, 6)

    def test_a_marker_pasted_by_someone_else_is_not_taken(self):
        preview = self._preview()
        client, patcher = self._client()
        client.get.return_value = [
            {'id': 5, 'body': preview._pr_note_marker(),
             'user': {'login': 'someone'}},
        ]
        with patcher:
            preview._job_pr_comment()
        client.post_issue_comment.assert_called_once()
        self.assertEqual(preview.pr_comment_id, 31)


class TestPreviewCommentFailures(_CommentCase):
    """Every way the comment's job can fail ends in an alert or a retry.

    An answer cut short, a JSON GitHub did not send whole, a token answer
    without its token ended the job failed, with no alert. A 401 with a
    token kept and since revoked, and GitHub's secondary rate limit, are
    worth another try.
    """

    def test_anything_else_raises_the_alert(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = KeyError('token')
        with patcher:
            preview._job_pr_comment()
        alert = self._note_alert(self.prod)
        self.assertEqual(len(alert), 1)
        self.assertIn('KeyError', alert.message)

    def test_a_401_is_tried_once_more_at_once(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = [
            GitHubAPIError(401, 'Bad credentials'), {'id': 31},
        ]
        with patcher:
            preview._job_pr_comment()
        self.assertEqual(client.post_issue_comment.call_count, 2)
        self.assertEqual(preview.pr_comment_id, 31)
        self.assertFalse(self._note_alert(self.prod))

    def test_a_second_401_raises_the_alert(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = GitHubAPIError(401, 'Bad credentials')
        Note = type(self.env['cloud.gitlab.mr.note'])
        with patcher, patch.object(Note, '_last_attempt', return_value=False):
            preview._job_pr_comment()
        self.assertEqual(client.post_issue_comment.call_count, 2)
        self.assertTrue(self._note_alert(self.prod))

    def test_the_secondary_rate_limit_is_tried_later(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = GitHubAPIError(
            403, 'You have exceeded a secondary rate limit',
        )
        Note = type(self.env['cloud.gitlab.mr.note'])
        with patcher, patch.object(Note, '_last_attempt', return_value=False), \
                self.assertRaises(RetryableJobError):
            preview._job_pr_comment()


class TestPreviewCommentAlertKey(_CommentCase):
    """The alert of a pull request's comment is that pull request's.

    Two pull requests previewing one production shared one alert: the
    comment of one that went through resolved the other's, and the close
    of the pull request left its alert open for good.
    """

    def test_two_pull_requests_have_an_alert_each(self):
        first = self._preview()
        second = self._preview(name='pr-8', pr_number=8)
        client, patcher = self._client()
        client.post_issue_comment.side_effect = GitHubAPIError(403, 'Forbidden')
        with patcher:
            first._job_pr_comment()
            second._job_pr_comment()
        alerts = self._note_alert(self.prod)
        self.assertEqual(len(alerts), 2)
        self.assertEqual(
            set(alerts.mapped('dedup_key')),
            {f'github_pr:{REPO_FULL}:7', f'github_pr:{REPO_FULL}:8'},
        )
        client.post_issue_comment.side_effect = None
        with patcher:
            second._job_pr_comment()
        self.assertEqual(
            self._note_alert(self.prod).dedup_key, f'github_pr:{REPO_FULL}:7',
        )

    def test_the_close_of_the_pull_request_resolves_it(self):
        preview = self._preview()
        client, patcher = self._client()
        client.post_issue_comment.side_effect = GitHubAPIError(403, 'Forbidden')
        with patcher:
            preview._job_pr_comment()
        self.assertTrue(self._note_alert(self.prod))
        Instance = type(self.env['cloud.instance'])
        with patch.object(Instance, '_remove_closed_preview'):
            self._event(action='closed')._process_pull_request_event()
        self.assertFalse(self._note_alert(self.prod))


class TestPreviewCommentDeleteLogs(_CommentCase):
    """A comment that cannot be deleted is a warning in the log, not an error:
    the App uninstalled from the account is the usual reason."""

    def test_a_refused_delete_is_a_warning(self):
        preview = self._preview(pr_comment_id=31)
        client, patcher = self._client()
        client.delete_issue_comment.side_effect = GitHubAPIError(404, 'Not Found')
        with patcher, self.assertLogs(cloud_instance._logger, 'WARNING') as logs:
            preview._delete_pr_comment()
        self.assertTrue(logs.records)
        self.assertTrue(all(r.levelname == 'WARNING' for r in logs.records))
        self.assertFalse(any(r.exc_info for r in logs.records))


class TestPullRequestOfAnotherInstallation(_PreviewBase):
    """A pull request through another installation of the App is said aloud.

    The panel acts on one installation of its App. A pull request through
    another one, on a repository a production of the panel follows, was
    ignored with a line in the log: whoever opened it waited for a preview
    that never came. An alert on that production says so now, naming the
    account, keyed by the installation; choosing that installation
    resolves it.
    """

    OTHER = INSTALLATION_ID + 1

    def _alerts(self):
        """Return the active alerts of another installation on production."""
        return self.env['cloud.alert'].search([
            ('code', '=', PR_OTHER_INSTALLATION_CODE),
            ('instance_id', '=', self.prod.id),
            ('state', '=', 'active'),
        ])

    def test_an_opened_pull_request_raises_a_warning_on_production(self):
        with self._clone_patch(return_value={}) as clone:
            self._event(installation=self.OTHER)._process_pull_request_event()
        clone.assert_not_called()
        alert = self._alerts()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'warning')
        self.assertEqual(alert.dedup_key, str(self.OTHER))
        self.assertIn(REPO_FULL.split('/')[0], alert.message)
        self.assertIn('#7', alert.message)
        self.assertIn('no preview', alert.message)
        self.assertNotIn('Check installation', alert.message)

    def test_a_repository_no_production_follows_makes_no_noise(self):
        self._event(
            installation=self.OTHER, base=(FORK_FULL, FORK_URL), head=(FORK_FULL, FORK_URL),
        )._process_pull_request_event()
        self.assertFalse(self.env['cloud.alert'].search([
            ('code', '=', PR_OTHER_INSTALLATION_CODE),
        ]))

    def test_a_fork_makes_no_noise(self):
        self._event(
            installation=self.OTHER, head=(FORK_FULL, FORK_URL),
        )._process_pull_request_event()
        self.assertFalse(self._alerts())

    def test_a_push_to_it_makes_no_noise(self):
        self._event(
            installation=self.OTHER, action='synchronize',
        )._process_pull_request_event()
        self.assertFalse(self._alerts())

    def test_choosing_that_installation_resolves_it(self):
        self._event(installation=self.OTHER)._process_pull_request_event()
        self.assertTrue(self._alerts())
        self.env['cloud.github.app'].search([]).write(
            {'installation_id': str(self.OTHER)},
        )
        self.assertFalse(self._alerts())
