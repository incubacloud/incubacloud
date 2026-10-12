"""Command-shape tests for ``FullSetupExecutor``.

full_setup is the convergence tool for a host's proxy stack: re-running
it must leave the host matching the compose file it just wrote. That
only holds if starting the stack also retires services the file no
longer declares — otherwise the run can add but never remove, which is
how a host kept serving a container long after the file dropped it.
"""
import asyncio
import re
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.acme_store import AcmeStorePruneMixin
from odoo.addons.incubacloud.models.full_setup_executor import (
    _TMP,
    FullSetupExecutor,
)
from odoo.addons.incubacloud.models.transport import SSHTransport


class TestFullSetupStartsTheStackConvergently(TransactionCase):

    def _commands(self):
        """Build the executor for a bare host and return its commands."""
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'full_setup')], limit=1,
        ) or self.env['cloud.job.type'].create({
            'name': 'full_setup', 'code': 'full_setup', 'apply_to': 'host',
        })
        host = self.env['cloud.host'].create({
            'name': 'fs-cmd-host',
            'ip_address': '10.0.0.9',
            'user': 'ubuntu',
            'wildcard_domain': 'fs-cmd.example.com',
        })
        job = self.env['cloud.job'].create({
            'host_id': host.id,
            'job_type_id': job_type.id,
            'name': 'Full Setup',
        })
        return dict(
            (c[0], c[1]) for c in FullSetupExecutor(job, host).get_commands()
        )

    def test_start_traefik_removes_orphans(self):
        cmd = self._commands()['Start Traefik']
        self.assertIn('--remove-orphans', cmd)

    def test_start_traefik_still_targets_the_inverseproxy_project(self):
        """The orphan sweep must stay scoped to this compose project."""
        cmd = self._commands()['Start Traefik']
        self.assertIn('-p inverseproxy', cmd)
        self.assertIn('inverseproxy.yaml', cmd)


class TestRebuildDefersOnBusyCrons(TransactionCase):
    """A rebuild blocked by a running cron must be rescheduled, not failed.

    Failing it would raise a ``job_failed`` alert — and notify the
    customer — for a collision that resolves itself: nothing was
    updated and the live stack was never touched.
    """

    def _executor(self):
        """Build a rebuild executor over a throwaway instance."""
        from odoo.addons.incubacloud.models.rebuild_instance_executor import (
            RebuildInstanceExecutor,
        )
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'rebuild_instance')], limit=1,
        ) or self.env['cloud.job.type'].create({
            'name': 'rebuild', 'code': 'rebuild_instance',
            'apply_to': 'instance',
        })
        host = self.env['cloud.host'].create({
            'name': 'cron-guard-host', 'ip_address': '10.0.0.12',
            'user': 'ubuntu', 'wildcard_domain': 'cg.example.com',
        })
        project = self.env['cloud.project'].create({'name': 'CronGuardP'})
        inst = self.env['cloud.instance'].create({
            'name': 'cronguardinst', 'project_id': project.id,
            'host_id': host.id, 'environment': 'production',
        })
        job = self.env['cloud.job'].create({
            'host_id': host.id, 'instance_id': inst.id,
            'job_type_id': job_type.id, 'name': 'Rebuild',
        })
        return RebuildInstanceExecutor(job, host)

    def test_busy_crons_raise_a_retryable_error(self):
        from odoo.addons.queue_job.exception import RetryableJobError
        with self.assertRaises(RetryableJobError):
            self._executor().parse_results({
                'Update changed modules': {'exit_status': 75, 'stdout': ''},
            })

    def test_a_real_update_failure_still_fails_the_job(self):
        """Only the guard's own code defers; a broken update must not."""
        errors = self._executor().parse_results({
            'Update changed modules': {'exit_status': 1, 'stdout': ''},
        })
        self.assertTrue(errors)

    def test_the_update_runs_through_the_cron_guard(self):
        cmds = dict(
            (c[0], c[1]) for c in self._executor().get_commands()
        )
        self.assertIn('cron_guard.sh', cmds['Update changed modules'])


class TestTheHostRendersItsProxyCompose(TransactionCase):
    """Full Setup uploads the compose file the host renders, as it does
    ``traefik.yml``: the one place a module extends (11-oct-2026)."""

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'fs-compose-host',
            'ip_address': '10.0.0.19',
            'user': 'ubuntu',
            'wildcard_domain': '*.fs-compose.example.com',
        })

    def test_the_dashboard_is_named_after_the_host(self):
        rendered = self.host._shipped_inverseproxy_yaml()
        self.assertIn('Host(`traefik.fs-compose.example.com`)', rendered)

    def test_full_setup_uploads_what_the_host_renders(self):
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'full_setup')], limit=1,
        ) or self.env['cloud.job.type'].create({
            'name': 'full_setup', 'code': 'full_setup', 'apply_to': 'host',
        })
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': job_type.id,
            'name': 'Full Setup',
        })
        transport = MagicMock(spec=SSHTransport)
        with patch.object(
            AcmeStorePruneMixin, '_prepare_acme_prune',
            autospec=True, return_value=None,
        ):
            asyncio.run(FullSetupExecutor(job, self.host).before_execute(transport))
        uploaded = {}
        for call in transport.upload_text_files.await_args_list:
            uploaded.update(call.args[0])
        # bcrypt salts every hash of the dashboard's password anew.
        salted = re.compile(r'\$\$2y\$\$12\$\$[^\s"]+')
        self.assertEqual(
            salted.sub('<hash>', uploaded[f'{_TMP}-inverseproxy.yaml']),
            salted.sub('<hash>', self.host._shipped_inverseproxy_yaml()),
        )
        self.assertEqual(
            uploaded[f'{_TMP}-traefik.yml'], self.host._shipped_traefik_yml(),
        )
