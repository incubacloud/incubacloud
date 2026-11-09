"""Command-shape tests for ``FullSetupExecutor``.

full_setup is the convergence tool for a host's proxy stack: re-running
it must leave the host matching the compose file it just wrote. That
only holds if starting the stack also retires services the file no
longer declares — otherwise the run can add but never remove, which is
how a host kept serving a container long after the file dropped it.
"""
import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.acme_store import AcmeStorePruneMixin
from odoo.addons.incubacloud.models.cloud_host_edge_guard import edge_guard_digest
from odoo.addons.incubacloud.models.full_setup_executor import (
    FullSetupExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

_PROXY_START = (
    Path(__file__).resolve().parents[1] / "scripts" / "proxy_start.sh"
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
        self.assertIn('proxy_start.sh start', cmd)
        self.assertIn('up -d --remove-orphans', _PROXY_START.read_text())

    def test_start_traefik_still_targets_the_inverseproxy_project(self):
        """The orphan sweep must stay scoped to this compose project."""
        self.assertIn(
            '-p inverseproxy -f inverseproxy.yaml', _PROXY_START.read_text(),
        )

    def test_config_is_copied_into_the_dynamic_directory_before_the_proxy_starts(self):
        """The file provider reads one directory; the middlewares every
        entrypoint names live in config.yml, so a proxy started without
        its copy there answers nothing."""
        commands = list(self._commands().items())
        labels = [label for label, _ in commands]
        copy_at = labels.index('Copy config.yml to dynamic directory')
        self.assertIn(
            'cp ~/traefik/config.yml ~/traefik/dynamic/config.yml',
            commands[copy_at][1],
        )
        self.assertLess(copy_at, labels.index('Start Traefik'))

    def test_the_templates_read_the_dynamic_directory(self):
        """The default certificate and the CDN's client-certificate
        requirement are documents of their own: only a directory provider
        picks them up, and only a container that mounts it can read it."""
        host = self.env['cloud.host'].create({
            'name': 'fs-tpl-host',
            'ip_address': '10.0.0.19',
            'user': 'ubuntu',
            'wildcard_domain': 'fs-tpl.example.com',
        })
        self.assertIn('directory: /etc/traefik/dynamic', host.traefik_yml)
        self.assertNotIn('filename: /etc/traefik/config.yml', host.traefik_yml)
        self.assertIn(
            './dynamic:/etc/traefik/dynamic:ro', host.traefik_inverseproxy_yaml,
        )

    def test_a_host_stored_with_the_single_file_provider_ships_the_directory(self):
        """Hosts from before 1.0.183 keep ``filename`` in the database;
        what they are sent is the directory form, and a compose file that
        mounts it, without the stored copy being rewritten."""
        host = self.env['cloud.host'].create({
            'name': 'fs-old-host',
            'ip_address': '10.0.0.29',
            'user': 'ubuntu',
            'wildcard_domain': 'fs-old.example.com',
        })
        host.write({
            'traefik_yml': host.traefik_yml.replace(
                'directory: /etc/traefik/dynamic',
                'filename: /etc/traefik/config.yml',
            ),
            'traefik_inverseproxy_yaml': host.traefik_inverseproxy_yaml.replace(
                '      - ./dynamic:/etc/traefik/dynamic:ro\n', '',
            ),
        })
        self.assertIn('filename: /etc/traefik/config.yml', host.traefik_yml)
        self.assertNotIn('./dynamic:', host.traefik_inverseproxy_yaml)
        shipped = host._shipped_traefik_yml()
        self.assertIn('directory: /etc/traefik/dynamic', shipped)
        self.assertNotIn('filename:', shipped)
        self.assertIn(
            '- ./dynamic:/etc/traefik/dynamic:ro', host._shipped_inverseproxy_yaml(),
        )
        # Rendering, not rewriting.
        self.assertIn('filename: /etc/traefik/config.yml', host.traefik_yml)

    def test_the_access_log_is_prepared_before_the_proxy_starts(self):
        """The proxy writes its access log into a directory mounted from
        the host; the rotation that keeps it to 30 days comes first."""
        commands = list(self._commands())
        self.assertIn('Prepare the proxy access log', commands)
        self.assertLess(
            commands.index('Prepare the proxy access log'),
            commands.index('Start Traefik'),
        )
        self.assertIn(
            'proxy_access_log.sh',
            self._commands()['Prepare the proxy access log'],
        )


class TestFullSetupWritesTheEdgeGuard(TransactionCase):
    """The proxy starts with the guard that refuses /website/info.

    The step is fatal, as the direct list is: a Full Setup that did not
    write it cannot end green, or every instance of the host would serve
    the page with nothing saying so (the silence closed on Tenants1,
    1-nov-2026). The proxy is neither mounted nor running yet, so the
    script checks neither.
    """

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'fs-guard-host',
            'ip_address': '10.0.0.39',
            'user': 'ubuntu',
            'wildcard_domain': 'fs-guard.example.com',
        })
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'full_setup')], limit=1,
        )
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': job_type.id,
            'name': 'Full Setup',
        })
        self.executor = FullSetupExecutor(self.job, self.host)

    def _uploaded(self):
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult('', 0)
        with patch.object(
            AcmeStorePruneMixin, '_prepare_acme_prune',
            autospec=True, return_value=None,
        ):
            asyncio.run(self.executor.before_execute(transport))
        uploaded = {}
        for call in transport.upload_text_files.await_args_list:
            uploaded.update(call.args[0])
        return uploaded

    def _good_results(self):
        results = {
            label: {'stdout': 'ok', 'exit_status': 0}
            for label, *_rest in self.executor.get_commands()
        }
        results['check:os']['stdout'] = 'Linux'
        results['check:disk']['stdout'] = str(100 * 1024 ** 3)
        return results

    def test_the_guard_is_staged(self):
        self.assertEqual(
            self._uploaded()[self.executor._job_path('ic_guard.yml')],
            self.host._shipped_edge_guard_yml(),
        )

    def test_it_is_written_into_the_directory_before_the_proxy_starts(self):
        commands = self.executor.get_commands()
        labels = [c[0] for c in commands]
        at = labels.index('Install the edge guard')
        self.assertGreater(at, labels.index('Copy config.yml to dynamic directory'))
        self.assertLess(at, labels.index('Start Traefik'))
        command = commands[at][1]
        self.assertIn(
            f"edge_guard.sh install {self.executor._job_path('ic_guard.yml')}",
            command,
        )
        # Not mounted and not running yet: neither is checked, so the
        # step never exits 3 or 4 and nothing is logged as failed.
        self.assertIn('--no-mount-check', command)
        self.assertIn('--no-probe', command)
        self.assertEqual(len(commands[at]), 2)

    def test_a_guard_that_was_not_written_fails_the_job(self):
        results = self._good_results()
        self.assertEqual(self.executor.parse_results(results), [])
        for status in (1, 3):
            with self.subTest(status=status):
                failed = {
                    **results,
                    'Install the edge guard': {'stdout': '', 'exit_status': status},
                }
                errors = self.executor.parse_results(failed)
                self.assertEqual(
                    [e for e in errors if 'Install the edge guard' in e],
                    [f"Traefik step 'Install the edge guard' failed (exit {status})"],
                )

    def test_success_records_the_guard_and_clears_its_alert(self):
        self.env['cloud.alert'].raise_alert(
            'edge_guard_not_mounted', 'not mounted', host=self.host,
        )
        with patch.object(
            type(self.host), '_enqueue_observability',
            autospec=True, return_value=False,
        ):
            asyncio.run(self.executor.on_success({}))
        self.assertEqual(
            self.host.edge_guard_shipped_hash,
            edge_guard_digest(self.host._shipped_edge_guard_yml()),
        )
        self.assertTrue(self.host.edge_guard_shipped_at)
        self.assertFalse(self.env['cloud.alert'].search([
            ('host_id', '=', self.host.id),
            ('code', '=', 'edge_guard_not_mounted'),
            ('state', '=', 'active'),
        ]))

    def test_a_host_the_guard_leaves_alone_gets_none(self):
        self.patch(
            type(self.host), '_edge_guard_leaves_alone', lambda host: True,
        )
        self.assertNotIn(
            self.executor._job_path('ic_guard.yml'), self._uploaded(),
        )
        labels = [c[0] for c in self.executor.get_commands()]
        self.assertNotIn('Install the edge guard', labels)
        results = self._good_results()
        self.assertEqual(self.executor.parse_results(results), [])
        with patch.object(
            type(self.host), '_enqueue_observability',
            autospec=True, return_value=False,
        ):
            asyncio.run(self.executor.on_success({}))
        self.assertFalse(self.host.edge_guard_shipped_hash)


class TestFullSetupGivesTheChecksVerdict(TransactionCase):
    """Full Setup's first phase measures what the Check does, and says it
    the same way: a host short of disk is ``degraded`` with
    ``host_low_disk``, not ``unsupported`` (its instances run on); only a
    host that is not Linux, or could not be read, is ``unsupported``. The
    run still fails, with its own ``setup_failed``."""

    def setUp(self):
        """A host and the Full Setup executor of a job on it."""
        self.registry_enter_test_mode()
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'fs-verdict-host',
            'ip_address': '10.0.0.41',
            'user': 'ubuntu',
            'wildcard_domain': 'fs-verdict.example.com',
        })
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': self.env['cloud.job.type'].search(
                [('code', '=', 'full_setup')], limit=1,
            ).id,
            'name': 'Full Setup',
        })
        self.executor = FullSetupExecutor(job, self.host)

    def _good_results(self):
        """Return what a run that went well printed, step by step."""
        return TestFullSetupWritesTheEdgeGuard._good_results(self)

    def _active(self, code):
        """Return the host's active alerts *code*."""
        return self.env['cloud.alert'].search([
            ('host_id', '=', self.host.id),
            ('code', '=', code),
            ('state', '=', 'active'),
        ])

    def _fail_with(self, **stdout):
        """Run parse_results and on_failure with these phase-1 outputs."""
        results = self._good_results()
        for label, out in stdout.items():
            results[label.replace('_', ':')]['stdout'] = out
        errors = self.executor.parse_results(results)
        self.assertTrue(errors)
        asyncio.run(self.executor.on_failure(results, errors))
        self.env.invalidate_all()
        return errors

    def test_little_disk_leaves_the_host_degraded(self):
        """7 GB free on Linux: degraded, its alert, and the setup failed."""
        self.host.write({'status': 'compatible'})
        self._fail_with(check_disk=str(7 * 1024 ** 3))
        self.assertEqual(self.host.status, 'degraded')
        alert = self._active('host_low_disk')
        self.assertEqual(alert.level, 'warning')
        self.assertIn('7.0 GB free', alert.message)
        self.assertTrue(self._active('setup_failed'))

    def test_not_linux_is_still_unsupported(self):
        """Not Linux is unsupported, whatever the disk."""
        self._fail_with(check_os='Darwin', check_disk=str(7 * 1024 ** 3))
        self.assertEqual(self.host.status, 'unsupported')
        self.assertFalse(self._active('host_low_disk'))
        self.assertTrue(self._active('setup_failed'))

    def test_a_disk_it_could_not_read_is_unsupported(self):
        """A reading that is not a number measured nothing."""
        self._fail_with(check_disk='not-a-number')
        self.assertEqual(self.host.status, 'unsupported')
        self.assertFalse(self._active('host_low_disk'))

    def test_success_resolves_the_checks_verdict(self):
        """It found Linux, the disk and every tool: the Check's alerts
        close, a missing-tools one dismissed by hand included."""
        for code in ('host_low_disk', 'host_unsupported'):
            self.env['cloud.alert'].raise_alert(code, 'found', host=self.host)
        self.env['cloud.alert'].raise_alert(
            'missing_deps', 'found', host=self.host,
        ).write({'state': 'dismissed', 'resolution': 'manual'})
        with patch.object(
            type(self.host), '_enqueue_observability',
            autospec=True, return_value=False,
        ):
            asyncio.run(self.executor.on_success({}))
        self.env.invalidate_all()
        self.assertFalse(self._active('host_low_disk'))
        self.assertFalse(self._active('host_unsupported'))
        self.assertEqual(self.host.status, 'compatible')
        self.assertFalse(self.env['cloud.alert'].search_count([
            ('host_id', '=', self.host.id),
            ('code', '=', 'missing_deps'),
            ('resolution', '=', 'manual'),
        ]))


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
        transport.run.return_value = CommandResult('', 0)
        executor = FullSetupExecutor(job, self.host)
        with patch.object(
            AcmeStorePruneMixin, '_prepare_acme_prune',
            autospec=True, return_value=None,
        ):
            asyncio.run(executor.before_execute(transport))
        uploaded = {}
        for call in transport.upload_text_files.await_args_list:
            uploaded.update(call.args[0])
        # bcrypt salts every hash of the dashboard's password anew.
        salted = re.compile(r'\$\$2y\$\$12\$\$[^\s"]+')
        self.assertEqual(
            salted.sub(
                '<hash>', uploaded[executor._job_path('inverseproxy.yaml')],
            ),
            salted.sub('<hash>', self.host._shipped_inverseproxy_yaml()),
        )
        self.assertEqual(
            uploaded[executor._job_path('traefik.yml')],
            self.host._shipped_traefik_yml(),
        )
