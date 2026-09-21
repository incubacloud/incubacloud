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
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.github.client import GitHubAppClient
from odoo.addons.incubacloud.models import cloud_github_event
from odoo.addons.incubacloud.models.cloud_github_event import (
    PR_PREVIEW_FAILED_CODE,
)

REPO_URL = 'https://github.com/owner/repo.git'
REPO_FULL = 'owner/repo'


def _pr_payload(pr_number, action='opened', branch='feature-x'):
    """Return the JSON a ``pull_request`` delivery carries."""
    return json.dumps({
        'action': action,
        'pull_request': {
            'number': pr_number,
            'head': {'ref': branch, 'repo': {'clone_url': REPO_URL}},
        },
        'repository': {'full_name': REPO_FULL},
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
        self.env['cloud.instance.repo'].create({
            'instance_id': self.prod.id,
            'url': REPO_URL,
            'branch': 'main',
        })

    def _event(self, pr_number=7, action='opened'):
        """Create an unprocessed ``pull_request`` event."""
        return self.env['cloud.github.event'].create({
            'event_type': 'pull_request',
            'delivery_id': f'pr-preview-{self.id()}-{pr_number}-{action}',
            'payload': _pr_payload(pr_number, action=action),
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
            raise UserError("A job is already running.")

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
