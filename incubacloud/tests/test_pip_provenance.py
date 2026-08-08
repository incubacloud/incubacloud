"""
Tier 2 — Dependency provenance and the pre-build requirements re-sync.

Covers the ORM half (who owns each pip line, and what happens to that
ownership when the field is written), the executor half (re-reading
each unpinned repo's ``requirements.txt`` in the job that pulls its
code, so a module shipped with a new library and the build that installs
it stay one event instead of two), the resolution endpoint (picking
a side of a conflict also moves the line's authority to that side), and
the thread-safe GitHub read the executor uses to get there.
"""
import asyncio
import base64
import json
from unittest.mock import MagicMock, patch

from odoo import api
from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.github.client import (
    GitHubAPIError,
    GitHubAppClient,
    GitHubPATClient,
)
from odoo.addons.incubacloud.github.credentials import GitHubAppCredentials
from odoo.addons.incubacloud.models._repo_requirements import (
    _normalize_url,
    create_pip_conflict_alert,
    fetch_requirements_txt_with_client,
    resolve_github_client,
)

_REPO_A = 'https://github.com/acme/repo-a'
_KEY_A = _normalize_url(_REPO_A)
_EXECUTOR_MODULE = 'odoo.addons.incubacloud.models.deploy_instance_executor'
_REQUIREMENTS_MODULE = 'odoo.addons.incubacloud.models._repo_requirements'
_MIXIN_FETCH = (
    'odoo.addons.incubacloud.models.cloud_repo_mixin.fetch_requirements_txt'
)


class ProvenanceCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({
            'name': 'Provenance Project',
            'pip_dependencies': 'libx==1.0\n',
        })


class TestProvenanceWriteHook(ProvenanceCase):
    """A hand edit of the list takes those lines back from upstream."""

    def setUp(self):
        super().setUp()
        self.project.pip_dependency_sources = {
            'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'},
        }

    def test_editing_a_line_drops_its_owner(self):
        # An empty Json field reads back as False, not {} — hence the
        # ``or {}`` guards everywhere the map is consumed.
        self.project.write({'pip_dependencies': 'libx==9.9\n'})
        self.assertFalse(self.project.pip_dependency_sources)

    def test_untouched_lines_keep_their_owner(self):
        self.project.write({'pip_dependencies': 'libx==1.0\nliby==2.0\n'})
        self.assertIn('libx', self.project.pip_dependency_sources)

    def test_managed_write_is_left_alone(self):
        """The merge owns the map it just computed; pruning would undo it."""
        self.project.with_context(pip_provenance_managed=True).write({
            'pip_dependencies': 'libx==2.0\n',
            'pip_dependency_sources': {
                'libx': {'repo': _KEY_A, 'spec': 'libx==2.0', 'label': 'a'},
            },
        })
        self.assertEqual(
            self.project.pip_dependency_sources['libx']['spec'], 'libx==2.0',
        )

    def test_write_without_pip_dependencies_does_not_prune(self):
        self.project.write({'name': 'Renamed'})
        self.assertIn('libx', self.project.pip_dependency_sources)


class TestApplyRequirementsRecordsProvenance(ProvenanceCase):
    """Creating a repo line claims the packages it brings."""

    def _create_repo(self, content):
        """Create a project repo line with *content* standing in for GitHub."""
        with patch(_MIXIN_FETCH, return_value=content):
            return self.env['cloud.project.repo'].create({
                'project_id': self.project.id,
                'url': _REPO_A,
                'branch': '19.0',
            })

    def test_new_package_is_stored_with_its_owner(self):
        self._create_repo('libnew==1.0\n')
        self.assertIn('libnew==1.0', self.project.pip_dependencies)
        sources = self.project.pip_dependency_sources
        self.assertEqual(sources['libnew']['repo'], _KEY_A)

    def test_matching_package_is_adopted_without_touching_the_text(self):
        self._create_repo('libx==1.0\n')
        self.assertEqual(self.project.pip_dependencies, 'libx==1.0\n')
        self.assertEqual(
            self.project.pip_dependency_sources['libx']['repo'], _KEY_A,
        )

    def test_upstream_bump_of_its_own_line_needs_no_human(self):
        repo = self._create_repo('libx==1.0\n')
        with patch(_MIXIN_FETCH, return_value='libx==1.5\n'):
            repo.write({'branch': '20.0'})
        self.assertIn('libx==1.5', self.project.pip_dependencies)
        self.assertNotIn('<<<<<<<', self.project.pip_dependencies)

    def test_operator_owned_line_is_never_overwritten(self):
        self._create_repo('libx==2.0\n')
        self.assertIn('<<<<<<<', self.project.pip_dependencies)
        self.assertIn('libx==1.0', self.project.pip_dependencies)


class TestInstanceInheritsProvenance(ProvenanceCase):
    """An instance created from a project inherits lines AND their authors."""

    def setUp(self):
        super().setUp()
        self.project.pip_dependency_sources = {
            'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'},
        }

    def test_map_travels_with_the_dependencies(self):
        instance = self.env['cloud.instance'].create({
            'name': 'prov-inst',
            'project_id': self.project.id,
            'environment': 'staging',
        })
        self.assertEqual(instance.pip_dependencies, 'libx==1.0\n')
        self.assertEqual(
            instance.pip_dependency_sources['libx']['repo'], _KEY_A,
        )

    def test_explicit_dependencies_do_not_drag_the_project_map(self):
        instance = self.env['cloud.instance'].create({
            'name': 'prov-inst-own',
            'project_id': self.project.id,
            'environment': 'staging',
            'pip_dependencies': 'libz==3.0\n',
        })
        self.assertFalse(instance.pip_dependency_sources)


class TestResyncExecutor(TransactionCase):
    """The re-sync step of deploy/rebuild.

    Persistence runs on its own cursor (so the merged list survives a
    later failure of the same job), which a test transaction cannot
    observe — the record it would write does not exist outside this
    transaction. The write is therefore asserted through the mocked
    cursor rather than by re-reading the instance.
    """

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({
            'name': 'Resync Project',
            'pip_dependencies': 'libx==1.0\n',
        })
        self.instance = self.env['cloud.instance'].create({
            'name': 'resync-inst',
            'project_id': self.project.id,
            'environment': 'staging',
            'pip_dependencies': 'libx==1.0\n',
        })
        self.repo = self.env['cloud.instance.repo'].with_context(
            skip_apply_requirements=True,
        ).create({
            'instance_id': self.instance.id,
            'url': _REPO_A,
            'branch': '19.0',
        })
        self.executor = self._make_executor()

    def _make_executor(self):
        """Build a deploy executor with no SSH and a mocked job record."""
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        executor.env = self.env
        executor._log_buffer = []
        job = MagicMock(spec=type(self.env['cloud.job']))
        job.id = 1
        job.instance_id = self.instance
        # ``self.job.env`` is only ever used to open the dedicated cursor
        # the persistence and alert paths write on. Spec'd against the
        # real Environment so a call outside its API fails here instead
        # of in production.
        job.env = MagicMock(spec=api.Environment)
        executor.job = job
        return executor

    def _run(self, fetched):
        """Run one re-sync with *fetched* standing in for GitHub."""
        with patch(
            f'{_EXECUTOR_MODULE}.resolve_github_client', return_value=None,
        ), patch(
            f'{_EXECUTOR_MODULE}.fetch_requirements_txt_with_client',
            return_value=fetched,
        ):
            return asyncio.run(self.executor._resync_repo_requirements())

    def _log(self):
        """Return the job log lines this run produced."""
        return [line for line, _source in self.executor._log_buffer]

    def _written_vals(self):
        """Return the vals handed to the write on the dedicated cursor."""
        env_factory = self.executor.job.env
        browse = env_factory.return_value.__getitem__.return_value.browse
        write = browse.return_value.sudo.return_value \
            .with_context.return_value.write
        self.assertTrue(write.called, "nothing was persisted")
        return write.call_args.args[0]

    def test_new_upstream_package_is_added_and_persisted(self):
        self._run('libnew==2.0\n')
        vals = self._written_vals()
        self.assertIn('libnew==2.0', vals['pip_dependencies'])
        self.assertEqual(
            vals['pip_dependency_sources']['libnew']['repo'], _KEY_A,
        )
        self.assertIn('  + libnew==2.0', self._log())

    def test_unreachable_github_keeps_the_stored_list(self):
        """A blip at GitHub must not stop the fleet from rebuilding."""
        self._run(None)
        self.assertIn(
            '· repo-a@19.0: no requirements.txt read — keeping the '
            'stored dependencies.',
            self._log(),
        )

    def test_conflict_files_an_alert_and_stops_the_job(self):
        with patch(f'{_EXECUTOR_MODULE}.create_pip_conflict_alert') as alert:
            with self.assertRaises(RuntimeError):
                self._run('libx==5.0\n')
        self.assertTrue(alert.called)
        conflicts = alert.call_args.args[1]
        self.assertEqual(conflicts[0]['name'], 'libx')
        self.assertEqual(
            alert.call_args.kwargs['instance_id'], self.instance.id,
        )

    def test_pinned_repo_is_left_frozen(self):
        """Frozen code, frozen dependencies."""
        self.repo.with_context(skip_apply_requirements=True).write({
            'commit_sha': 'a' * 40,
        })
        self._run('libnew==2.0\n')
        self.assertEqual(self._log(), [])

    def test_kill_switch_skips_the_whole_step(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'incubacloud.requirements_resync_enabled', '0',
        )
        self._run('libnew==2.0\n')
        self.assertEqual(self._log(), [])

    def test_already_in_sync_says_so_and_writes_nothing(self):
        self.instance.with_context(pip_provenance_managed=True).write({
            'pip_dependency_sources': {
                'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'},
            },
        })
        self._run('libx==1.0\n')
        self.assertIn(
            '✓ Dependencies already in sync with the repos.', self._log(),
        )

    def test_dropped_upstream_package_is_traced_in_the_log(self):
        """Upstream letting a package go is reported, never applied."""
        self.instance.with_context(pip_provenance_managed=True).write({
            'pip_dependency_sources': {
                'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'},
            },
        })
        self._run('libother==2.0\n')
        self.assertIn(
            '· repo-a@19.0: libx is gone from upstream requirements '
            '— kept, dependencies are never auto-removed.',
            self._log(),
        )
        vals = self._written_vals()
        self.assertIn('libx==1.0', vals['pip_dependencies'])
        self.assertIn('libx', vals['pip_dependency_sources'])


class TestResolveConflictOwnership(TransactionCase):
    """Resolving a conflict moves the line's authority to the chosen side.

    Same harness as the other controller tests: the endpoint is called
    on a bare ``CrudMixin`` with ``request`` patched to a spec'd fake
    whose env is the test transaction.
    """

    _MARKER = (
        '<<<<<<< existing\n'
        'libx==1.0\n'
        '=======\n'
        'libx==2.0\n'
        '>>>>>>> acme/repo-a (19.0)'
    )

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({
            'name': 'Resolve Project',
            'pip_dependencies': self._MARKER + '\n',
        })
        self.env['cloud.project.repo'].with_context(
            skip_apply_requirements=True,
        ).create({
            'project_id': self.project.id,
            'url': _REPO_A,
            'branch': '19.0',
        })
        self.alert = create_pip_conflict_alert(
            self.env,
            [{
                'name': 'libx',
                'existing': 'libx==1.0',
                'existing_source': 'existing',
                'incoming': 'libx==2.0',
                'incoming_source': 'acme/repo-a (19.0)',
            }],
            project_id=self.project.id,
        )
        self.controller = _routes_crud.CrudMixin()
        # Manager-gated via ``self._sec()``; under TransactionCase the
        # env is superuser so the group check passes on the bare mixin.
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _resolve(self, chosen):
        """Resolve the libx conflict with *chosen* through the endpoint."""
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_crud, 'request', fake_req):
            return self.controller.cloud_resolve_pip_conflict(
                self.alert.id, {'libx': chosen},
            )

    def test_picking_the_repo_side_hands_the_line_to_it(self):
        result = self._resolve('libx==2.0')
        self.assertTrue(result['ok'])
        self.assertEqual(self.project.pip_dependencies, 'libx==2.0\n')
        sources = self.project.pip_dependency_sources
        self.assertEqual(sources['libx']['repo'], _KEY_A)
        self.assertEqual(sources['libx']['spec'], 'libx==2.0')
        self.assertEqual(self.alert.state, 'dismissed')

    def test_keeping_the_stored_spec_makes_the_line_the_operators(self):
        # Seed an upstream owner so the pop is observable.
        self.project.with_context(pip_provenance_managed=True).write({
            'pip_dependency_sources': {
                'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'},
            },
        })
        result = self._resolve('libx==1.0')
        self.assertTrue(result['ok'])
        self.assertEqual(self.project.pip_dependencies, 'libx==1.0\n')
        self.assertFalse(self.project.pip_dependency_sources)


def _payload(text):
    """Return a GitHub contents API payload carrying *text*."""
    return {'content': base64.b64encode(text.encode()).decode()}


class TestFetchRequirementsWithClient(BaseCase):
    """The thread-safe read the re-sync depends on.

    Every failure here is silent by design — the caller treats ``None``
    as "no manifest" and keeps the stored list — so a bug in this
    function would turn the whole re-sync into a no-op that logs nothing
    wrong. That is precisely the shape of the incident this feature
    exists to prevent, hence the direct coverage.
    """

    def test_content_is_decoded_from_the_api_payload(self):
        client = MagicMock(spec=GitHubPATClient)
        client.get.return_value = _payload('libx==1.0\n')
        result = fetch_requirements_txt_with_client(client, _REPO_A, '19.0')
        self.assertEqual(result, 'libx==1.0\n')
        self.assertIn('/repos/acme/repo-a/contents/requirements.txt',
                      client.get.call_args.args[0])
        self.assertIn('ref=19.0', client.get.call_args.args[0])

    def test_unparseable_url_is_not_even_requested(self):
        client = MagicMock(spec=GitHubPATClient)
        result = fetch_requirements_txt_with_client(
            client, 'https://gitlab.com/acme/repo-a', '19.0',
        )
        self.assertIsNone(result)
        client.get.assert_not_called()

    def test_missing_file_stops_without_the_anonymous_retry(self):
        """404 is an answer, not a failure: the repo has no manifest."""
        client = MagicMock(spec=GitHubPATClient)
        client.get.side_effect = GitHubAPIError(404, 'Not Found')
        with patch(f'{_REQUIREMENTS_MODULE}.safe_urlopen',
                   autospec=True) as urlopen:
            result = fetch_requirements_txt_with_client(
                client, _REPO_A, '19.0',
            )
        self.assertIsNone(result)
        urlopen.assert_not_called()

    def test_broken_client_falls_back_to_the_public_read(self):
        """A bad token must not hide a public repo's requirements."""
        client = MagicMock(spec=GitHubPATClient)
        client.get.side_effect = GitHubAPIError(401, 'Bad credentials')
        with patch(f'{_REQUIREMENTS_MODULE}.safe_urlopen',
                   autospec=True) as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = (
                json.dumps(_payload('libpublic==2.0\n')).encode()
            )
            result = fetch_requirements_txt_with_client(
                client, _REPO_A, '19.0',
            )
        self.assertEqual(result, 'libpublic==2.0\n')

    def test_no_credentials_still_reads_a_public_repo(self):
        with patch(f'{_REQUIREMENTS_MODULE}.safe_urlopen',
                   autospec=True) as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = (
                json.dumps(_payload('libpublic==2.0\n')).encode()
            )
            result = fetch_requirements_txt_with_client(None, _REPO_A, '19.0')
        self.assertEqual(result, 'libpublic==2.0\n')

    def test_unreachable_github_returns_none(self):
        with patch(f'{_REQUIREMENTS_MODULE}.safe_urlopen',
                   autospec=True) as urlopen:
            urlopen.side_effect = OSError('connection refused')
            result = fetch_requirements_txt_with_client(None, _REPO_A, '19.0')
        self.assertIsNone(result)


class TestResolveGithubClient(TransactionCase):
    """Credentials are resolved on the main thread, before any HTTP."""

    def setUp(self):
        super().setUp()
        self.service_cls = type(self.env['cloud.github.credential.service'])

    def test_a_configured_pat_wins(self):
        with patch.object(self.service_cls, 'get_pat', return_value='ghp_x'):
            client = resolve_github_client(self.env)
        self.assertIsInstance(client, GitHubPATClient)

    def test_without_a_pat_the_app_is_used(self):
        with patch.object(self.service_cls, 'get_pat', return_value=None), \
                patch.object(self.service_cls, 'get_credentials',
                             return_value=MagicMock(spec=GitHubAppCredentials)):
            client = resolve_github_client(self.env)
        self.assertIsInstance(client, GitHubAppClient)

    def test_no_credentials_resolves_to_none(self):
        """Callers then read public repos anonymously rather than fail."""
        with patch.object(self.service_cls, 'get_pat', return_value=None), \
                patch.object(self.service_cls, 'get_credentials',
                             side_effect=UserError('not configured')):
            client = resolve_github_client(self.env)
        self.assertIsNone(client)
