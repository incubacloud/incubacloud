"""Command-shape tests for ``FullSetupExecutor``.

full_setup is the convergence tool for a host's proxy stack: re-running
it must leave the host matching the compose file it just wrote. That
only holds if starting the stack also retires services the file no
longer declares — otherwise the run can add but never remove, which is
how a host kept serving a container long after the file dropped it.
"""
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.full_setup_executor import (
    FullSetupExecutor,
)


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
