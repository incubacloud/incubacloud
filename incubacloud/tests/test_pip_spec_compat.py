"""Pip lines that agree are no conflict, and a resolved conflict stays resolved.

Seen on an Odoo 18 instance of the panel itself (9-nov-2026): core 18.0
pins ``requests==2.34.2`` and OCA/queue 18.0 asks for ``requests``. The
re-sync before each deploy called that a conflict. Resolving it in the
dialog did not hold: siding with the pin dropped the line's owner, core
adopted it again at the next re-sync, OCA/queue's bare name raised the
same conflict, the job failed again, and its ``job_failed`` kept the
instance's queue held after the conflict alert was closed.
"""
import asyncio
from typing import ClassVar
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.controllers.data_load import (
    CloudDataLoadController,
)
from odoo.addons.incubacloud.models._repo_requirements import (
    _normalize_url,
    create_pip_conflict_alert,
    hand_resolved_pip_sources,
    merge_pip_requirements,
    pip_spec_relation,
)
from odoo.addons.incubacloud.models.cloud_security_mixin import (
    CloudSecurityMixin,
)
from odoo.addons.incubacloud.tests.test_executor_durable_writes import (
    JobEndAssertions,
)
from odoo.addons.incubacloud.tests.test_job_queue import _QueueCase

_CORE = 'https://github.com/incubacloud/incubacloud'
_QUEUE = 'https://github.com/oca/queue'
_EXECUTOR_MODULE = 'odoo.addons.incubacloud.models.deploy_instance_executor'


class TestSpecRelation(BaseCase):
    """When two lines of one package can both hold, and which one to keep."""

    def test_lines_that_agree(self):
        cases = [
            ('requests==2.34.2', 'requests', 'existing'),
            ('requests', 'requests==2.34.2', 'incoming'),
            ('requests==2.34.2', 'requests>=2.30', 'existing'),
            ('requests>=2.30', 'requests==2.34.2', 'incoming'),
            ('requests>=2.30,<3', 'requests>=2.30', 'existing'),
            ('requests[socks]==2.34.2', 'requests', 'existing'),
            ('requests == 2.34.2', 'requests==2.34.2', 'existing'),
        ]
        for existing, incoming, expected in cases:
            with self.subTest(existing=existing, incoming=incoming):
                self.assertEqual(
                    pip_spec_relation(existing, incoming), expected,
                )

    def test_lines_that_disagree(self):
        cases = [
            ('requests==2.34.2', 'requests==2.30'),
            ('requests>=2.30', 'requests==2.0'),
            ('requests>=2.30', 'requests<3'),
            ("requests==2.34.2; python_version>'3'", 'requests'),
            ('requests', "requests==2.34.2; python_version>'3'"),
            ("requests; python_version>'3'", 'requests'),
            ('requests==2.0', 'requests @ git+https://github.com/o/requests.git'),
            # A URL is not a version: neither side can tell it meets a
            # bare name, so a person decides, as the editors do.
            ('pkg @ git+https://github.com/o/pkg.git', 'pkg'),
            ('pkg', 'pkg @ git+https://github.com/o/pkg.git'),
            ('--hash=sha256:00', 'requests'),
        ]
        for existing, incoming in cases:
            with self.subTest(existing=existing, incoming=incoming):
                self.assertIsNone(pip_spec_relation(existing, incoming))


class TestMergeKeepsWhatAgrees(BaseCase):
    """The merge only marks lines that cannot both hold."""

    def test_a_bare_name_is_met_by_the_pin(self):
        result = merge_pip_requirements(
            'requests==2.34.2\n', 'requests\n', _QUEUE, '18.0', sources={},
        )
        self.assertEqual(result['content'], 'requests==2.34.2\n')
        self.assertFalse(result['conflicts'])
        self.assertEqual(result['satisfied'], [{
            'name': 'requests', 'existing': 'requests==2.34.2',
            'incoming': 'requests',
        }])

    def test_a_pin_replaces_the_bare_name_it_meets(self):
        result = merge_pip_requirements(
            'requests\nlibx==1.0\n', 'requests==2.34.2\n', _CORE, '18.0',
            sources={},
        )
        self.assertEqual(result['content'], 'requests==2.34.2\nlibx==1.0\n')
        self.assertFalse(result['conflicts'])
        self.assertEqual(
            result['sources']['requests']['repo'], _normalize_url(_CORE),
        )

    def test_two_pins_still_conflict(self):
        result = merge_pip_requirements(
            'requests==2.34.2\n', 'requests==2.30\n', _QUEUE, '18.0',
            sources={},
        )
        self.assertEqual(len(result['conflicts']), 1)
        self.assertIn('<<<<<<< existing', result['content'])

    def test_a_line_turned_down_is_not_asked_again(self):
        sources = {'requests': {
            'repo': '', 'spec': 'requests==2.34.2', 'label': 'operator',
            'rejected': ['requests==2.30'],
        }}
        result = merge_pip_requirements(
            'requests==2.34.2\n', 'requests==2.30\n', _QUEUE, '18.0',
            sources=sources,
        )
        self.assertFalse(result['conflicts'])
        self.assertEqual(result['content'], 'requests==2.34.2\n')
        self.assertEqual(result['kept'][0]['incoming'], 'requests==2.30')

    def test_a_new_line_from_the_same_repo_is_asked(self):
        """What was turned down is one line, not the repo's every line."""
        sources = {'requests': {
            'repo': '', 'spec': 'requests==2.34.2', 'label': 'operator',
            'rejected': ['requests==2.30'],
        }}
        result = merge_pip_requirements(
            'requests==2.34.2\n', 'requests==2.31\n', _QUEUE, '18.0',
            sources=sources,
        )
        self.assertEqual(len(result['conflicts']), 1)

    def test_an_operator_range_is_not_replaced_by_a_pin(self):
        """``foo<3`` typed by a person stays theirs against a repo's pin.

        Replaced, the line became the repo's, and its next bump to
        ``foo==3.0`` went in without asking: the range was lost.
        """
        answered = {'foo': {
            'repo': '', 'spec': 'foo<3', 'label': 'operator',
        }}
        for sources in ({}, answered, None):
            with self.subTest(sources=sources):
                result = merge_pip_requirements(
                    'foo<3\n', 'foo==2.9\n', _CORE, '18.0', sources=sources,
                )
                self.assertEqual(len(result['conflicts']), 1)
                self.assertIn('<<<<<<< existing\nfoo<3\n', result['content'])
                self.assertFalse(result['updated'])

    def test_another_repo_range_is_not_replaced_by_a_pin(self):
        """A range another repo wrote, or adopted verbatim, is asked about.

        Taken by the pin, the line became that repo's, and its next bump
        past the range (``foo==3.0``) went in against the other repo.
        """
        adopted = merge_pip_requirements(
            'foo<3\n', 'foo<3\n', _QUEUE, '18.0', sources={},
        )
        self.assertEqual(adopted['adopted'], ['foo'])
        result = merge_pip_requirements(
            adopted['content'], 'foo==2.9\n', _CORE, '18.0',
            sources=adopted['sources'],
        )
        self.assertEqual(len(result['conflicts']), 1)
        self.assertIn('<<<<<<< existing\nfoo<3\n', result['content'])
        self.assertEqual(
            result['sources']['foo']['repo'], _normalize_url(_QUEUE),
        )

    def test_a_commented_bare_name_gives_way_to_the_pin(self):
        """The line is replaced, comment kept, not only logged as updated."""
        result = merge_pip_requirements(
            'requests  # needed by X\nlibx==1.0\n', 'requests==2.34.2\n',
            _CORE, '18.0', sources={},
        )
        self.assertEqual(
            result['content'], 'requests==2.34.2  # needed by X\nlibx==1.0\n',
        )
        self.assertEqual(result['updated'], [{
            'name': 'requests', 'old': 'requests', 'new': 'requests==2.34.2',
        }])
        self.assertEqual(result['sources']['requests']['spec'], 'requests==2.34.2')

    def test_a_commented_or_indented_line_gets_its_conflict_markers(self):
        for stored in ('requests==2.0  # pinned for X', '  requests==2.0'):
            with self.subTest(stored=stored):
                result = merge_pip_requirements(
                    f'{stored}\nlibx==1.0\n', 'requests==2.30\n', _QUEUE,
                    '18.0', sources={},
                )
                self.assertEqual(len(result['conflicts']), 1)
                self.assertTrue(result['content'].startswith(
                    '<<<<<<< existing\nrequests==2.0\n=======\n'
                    'requests==2.30\n>>>>>>> oca/queue (18.0)\nlibx==1.0\n',
                ), result['content'])

    def test_a_line_that_becomes_the_spec_is_no_longer_turned_down(self):
        """The owner bumping to a line once turned down takes it off the list."""
        sources = {'requests': {
            'repo': _normalize_url(_CORE), 'spec': 'requests==2.34.2',
            'label': 'incubacloud/incubacloud (18.0)',
            'rejected': ['requests==2.30', 'requests==2.0'],
        }}
        result = merge_pip_requirements(
            'requests==2.34.2\n', 'requests==2.30\n', _CORE, '18.0',
            sources=sources,
        )
        self.assertEqual(result['content'], 'requests==2.30\n')
        entry = result['sources']['requests']
        self.assertEqual(entry['spec'], 'requests==2.30')
        self.assertEqual(entry['rejected'], ['requests==2.0'])


class TestHandEditAnswers(BaseCase):
    """Only a hand edit that leaves one of the two lines is an answer."""

    BLOCK = (
        '<<<<<<< existing\nfoo==1\n=======\nfoo==2\n'
        '>>>>>>> oca/queue (18.0)'
    )

    def _answer(self, new_text):
        """The map a hand edit of ``BLOCK`` into *new_text* leaves."""
        return hand_resolved_pip_sources(
            f'{self.BLOCK}\nlibx==1.0', new_text, [], {},
        )

    def test_leaving_one_line_is_an_answer(self):
        self.assertEqual(self._answer('foo==1\nlibx==1.0'), {'foo': {
            'repo': '', 'spec': 'foo==1', 'label': 'operator',
            'rejected': ['foo==2'],
        }})

    def test_markers_removed_without_choosing_are_no_answer(self):
        for new_text in (
            'foo==1\nfoo==2\nlibx==1.0',
            '<<<<<<< existing\nfoo==1\nlibx==1.0',
            'foo==1\n=======\nlibx==1.0',
            'foo==1\n>>>>>>> oca/queue (18.0)\nlibx==1.0',
        ):
            with self.subTest(new_text=new_text):
                self.assertEqual(self._answer(new_text), {})


class _TwoRepoResync(JobEndAssertions, TransactionCase):
    """An instance with core 18.0 and OCA/queue 18.0, re-synced as a deploy does."""

    #: What each repository's ``requirements.txt`` says.
    FETCHED: ClassVar[dict] = {
        _CORE: 'requests==2.34.2\nurllib3==2.8.0\n',
        _QUEUE: 'requests\n',
    }

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        project = self.env['cloud.project'].create({'name': 'ic project'})
        self.instance = self.env['cloud.instance'].create({
            'name': 'ic',
            'project_id': project.id,
            'environment': 'staging',
            'pip_dependencies': 'urllib3==2.8.0\nrequests==2.34.2',
        })
        Repo = self.env['cloud.instance.repo'].with_context(
            skip_apply_requirements=True,
        )
        for seq, url in enumerate((_CORE, _QUEUE), start=10):
            Repo.create({
                'instance_id': self.instance.id, 'url': url,
                'branch': '18.0', 'sequence': seq,
            })
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env['cloud.security.mixin']

    def _executor(self):
        """A deploy executor with no SSH and a spec'd job record."""
        from odoo.addons.incubacloud.models.deploy_instance_executor import (
            DeployInstanceExecutor,
        )

        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        executor.env = self.env
        executor._log_buffer = []
        job = MagicMock(spec=type(self.env['cloud.job']))
        # No job row: the conflict alert is then filed without one.
        job.id = False
        job.instance_id = self.instance
        job.env = self.env
        executor.job = job
        self.env.flush_all()
        return executor

    def _resync(self):
        """Run one re-sync with ``FETCHED`` standing in for GitHub.

        :return: the job log lines
        """
        executor = self._executor()
        with patch(
            f'{_EXECUTOR_MODULE}.resolve_github_client', return_value=None,
        ), patch(
            f'{_EXECUTOR_MODULE}.fetch_requirements_txt_with_client',
            side_effect=lambda _client, url, _branch: self.FETCHED[url],
        ):
            asyncio.run(executor._resync_repo_requirements())
        self.instance.invalidate_recordset()
        return [line for line, _source in executor._log_buffer]

    def _resolve(self, alert, choices):
        """Answer *alert* with *choices* through the dialog's endpoint."""
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_crud, 'request', fake_req):
            return self.controller.cloud_resolve_pip_conflict(
                alert.id, choices,
            )

    def _alert(self):
        """The instance's active pip_conflict alert, if any."""
        return self.env['cloud.alert'].search([
            ('instance_id', '=', self.instance.id),
            ('code', '=', 'pip_conflict'), ('state', '=', 'active'),
        ])


class TestCoreAndQueueAgree(_TwoRepoResync):

    def test_the_bare_name_raises_no_conflict(self):
        log = self._resync()
        self.assertFalse(self._alert())
        self.assertNotIn('<<<<<<<', self.instance.pip_dependencies)
        self.assertIn(
            '· queue@18.0: requests is already met by requests==2.34.2.', log,
        )

    def test_and_again_at_the_next_deploy(self):
        self._resync()
        self._resync()
        self.assertFalse(self._alert())
        self.assertIn('requests==2.34.2', self.instance.pip_dependencies)
        # Core wrote the pin, so core's next bump of it needs no person.
        self.assertEqual(
            self.instance.pip_dependency_sources['requests']['repo'],
            _normalize_url(_CORE),
        )


class TestResolutionHolds(_TwoRepoResync):
    """A real conflict, answered once, is not asked again."""

    FETCHED: ClassVar[dict] = {
        _CORE: 'requests==2.34.2\n',
        _QUEUE: 'requests==2.30\n',
    }

    def _conflict(self):
        """Leave the instance as the first re-sync left ic, with its alert.

        Core adopted its pin, OCA/queue's line went into markers. Built
        here rather than by a re-sync that fails: under the test cursor
        a job that raises does not keep its durable writes.
        """
        self.instance.with_context(pip_provenance_managed=True).write({
            'pip_dependencies': (
                'urllib3==2.8.0\n<<<<<<< existing\nrequests==2.34.2\n'
                '=======\nrequests==2.30\n>>>>>>> oca/queue (18.0)'
            ),
            'pip_dependency_sources': {
                'requests': {
                    'repo': _normalize_url(_CORE), 'spec': 'requests==2.34.2',
                    'label': 'incubacloud/incubacloud (18.0)',
                },
            },
        })
        return create_pip_conflict_alert(self.env, [{
            'name': 'requests', 'existing': 'requests==2.34.2',
            'existing_source': 'existing', 'incoming': 'requests==2.30',
            'incoming_source': 'oca/queue (18.0)',
        }], instance_id=self.instance.id)

    def test_keeping_the_stored_pin_holds_at_the_next_deploy(self):
        alert = self._conflict()
        answer = self._resolve(alert, {'requests': 'requests==2.34.2'})
        self.assertTrue(answer['ok'])
        log = self._resync()
        self.assertFalse(self._alert())
        self.assertIn('requests==2.34.2', self.instance.pip_dependencies)
        self.assertNotIn('<<<<<<<', self.instance.pip_dependencies)
        self.assertIn(
            '· queue@18.0: requests==2.30 was turned down when a conflict '
            'was resolved — keeping requests==2.34.2.',
            log,
        )
        # Core wrote the pin kept: its next bump still needs no person.
        entry = self.instance.pip_dependency_sources['requests']
        self.assertEqual(entry['repo'], _normalize_url(_CORE))
        self.assertEqual(entry['rejected'], ['requests==2.30'])

    def test_siding_with_the_repo_holds_at_the_next_deploy(self):
        alert = self._conflict()
        self._resolve(alert, {'requests': 'requests==2.30'})
        self._resync()
        self.assertFalse(self._alert())
        self.assertIn('requests==2.30', self.instance.pip_dependencies)
        self.assertNotIn('requests==2.34.2', self.instance.pip_dependencies)
        entry = self.instance.pip_dependency_sources['requests']
        self.assertEqual(entry['repo'], _normalize_url(_QUEUE))
        self.assertEqual(entry['rejected'], ['requests==2.34.2'])

    def test_a_hand_resolution_holds_at_the_next_deploy(self):
        """Edit Manually: leaving one side of the markers is an answer too."""
        self._conflict()
        self.instance.write({
            'pip_dependencies': 'urllib3==2.8.0\nrequests==2.34.2  # core',
        })
        entry = self.instance.pip_dependency_sources['requests']
        self.assertEqual(entry['repo'], _normalize_url(_CORE))
        self.assertEqual(entry['rejected'], ['requests==2.30'])
        # The re-sync raises on a conflict: going through is the answer
        # holding. (The alert itself is closed by the deploy, not here.)
        log = self._resync()
        self.assertNotIn('<<<<<<<', self.instance.pip_dependencies)
        self.assertIn(
            '· queue@18.0: requests==2.30 was turned down when a conflict '
            'was resolved — keeping requests==2.34.2.',
            log,
        )

    def test_a_hand_resolution_for_the_repo_hands_it_the_line(self):
        self._conflict()
        self.instance.write({
            'pip_dependencies': 'urllib3==2.8.0\nrequests==2.30',
        })
        entry = self.instance.pip_dependency_sources['requests']
        self.assertEqual(entry['repo'], _normalize_url(_QUEUE))
        self.assertEqual(entry['rejected'], ['requests==2.34.2'])
        self._resync()
        self.assertNotIn('<<<<<<<', self.instance.pip_dependencies)
        self.assertNotIn('requests==2.34.2', self.instance.pip_dependencies)

    def test_answering_one_conflict_by_hand_keeps_the_other_s_owner(self):
        """A package still in its markers keeps its owner and its answers."""
        bar = {
            'repo': _normalize_url(_QUEUE), 'spec': 'bar==1.0',
            'label': 'oca/queue (18.0)', 'rejected': ['bar==0.5'],
        }
        self.instance.with_context(pip_provenance_managed=True).write({
            'pip_dependencies': (
                '<<<<<<< existing\nfoo==1\n=======\nfoo==2\n'
                '>>>>>>> oca/queue (18.0)\n'
                '<<<<<<< existing\nbar==1.0\n=======\nbar==2.0\n'
                '>>>>>>> incubacloud/incubacloud (18.0)'
            ),
            'pip_dependency_sources': {'bar': bar},
        })
        self.instance.write({'pip_dependencies': (
            'foo==1\n'
            '<<<<<<< existing\nbar==1.0\n=======\nbar==2.0\n'
            '>>>>>>> incubacloud/incubacloud (18.0)'
        )})
        sources = self.instance.pip_dependency_sources
        self.assertEqual(sources['bar'], bar)
        self.assertEqual(sources['foo']['rejected'], ['foo==2'])

    def test_a_third_line_typed_by_hand_is_no_answer(self):
        """Neither side left: nothing is remembered, the line is the operator's."""
        self._conflict()
        self.instance.write({
            'pip_dependencies': 'urllib3==2.8.0\nrequests==2.33.0',
        })
        self.assertNotIn(
            'requests', self.instance.pip_dependency_sources or {},
        )

    def test_a_hand_edit_of_the_line_forgets_the_answer(self):
        """Editing the line is a new decision: the old one goes with it."""
        alert = self._conflict()
        self._resolve(alert, {'requests': 'requests==2.34.2'})
        self.instance.write({
            'pip_dependencies': 'urllib3==2.8.0\nrequests==2.33.0',
        })
        self.assertNotIn(
            'requests', self.instance.pip_dependency_sources or {},
        )


class TestResolvingClosesTheFailure(_QueueCase):
    """The job that failed on a conflict stops holding once it is answered."""

    _MARKER = (
        '<<<<<<< existing\nrequests==2.34.2\n=======\nrequests==2.30\n'
        '>>>>>>> oca/queue (18.0)'
    )

    def setUp(self):
        super().setUp()
        self.inst.sudo().with_context(pip_provenance_managed=True).write({
            'pip_dependencies': self._MARKER,
        })
        self.failed = self._job('rebuild_instance', state='failed')
        self.conflict = create_pip_conflict_alert(
            self.env,
            [{
                'name': 'requests', 'existing': 'requests==2.34.2',
                'existing_source': 'existing', 'incoming': 'requests==2.30',
                'incoming_source': 'oca/queue (18.0)',
            }],
            instance_id=self.inst.id, job_id=self.failed.id,
        )
        self.Job._create_job_failed_alert(
            self.failed, exc_message='Pre-flight: 1 pip dependency conflict(s)',
        )
        self.failure = self.env['cloud.alert'].search([
            ('job_id', '=', self.failed.id), ('code', '=', 'job_failed'),
            ('state', '=', 'active'),
        ])
        self.assertTrue(self.failure, 'a warning conflict leaves a job_failed')

    def test_the_dialog_closes_both(self):
        controller = CloudDataLoadController()
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_crud, 'request', fake_req), patch.object(
            CloudDataLoadController, '_sec', autospec=True,
            return_value=MagicMock(spec=CloudSecurityMixin),
        ) as sec:
            answer = controller.cloud_resolve_pip_conflict(
                self.conflict.id, {'requests': 'requests==2.34.2'},
            )
        sec.return_value._check_cloud_group.assert_called_once_with(
            'group_cloud_consultant',
        )
        self.assertTrue(answer['ok'])
        self.assertEqual(self.conflict.state, 'dismissed')
        self.assertEqual(self.failure.state, 'dismissed')
        self.assertEqual(self.failure.resolution, 'manual')
        self.assertFalse(self.Job._queue_held_by(self.inst.id, None))

    def test_a_deploy_that_finds_the_list_clean_closes_both(self):
        self.inst.sudo().write({'pip_dependencies': 'requests==2.34.2'})
        self._enqueue('rebuild_instance')
        self.assertEqual(self.conflict.state, 'dismissed')
        self.assertEqual(self.failure.state, 'dismissed')
        self.assertEqual(self.failure.resolution, 'auto')

    def test_a_failure_of_another_job_stays(self):
        other, other_alert = self._failed_with_alert('restore_instance')
        self.conflict.write({'state': 'dismissed', 'resolution': 'manual'})
        self.assertEqual(self.failure.state, 'dismissed')
        self.assertEqual(other_alert.state, 'active')
        self.assertTrue(other)
