"""Tests for ``cloud.host.terminal.route``.

The routing schema and liveness/GC/resolve logic is inherited verbatim
from the shared ``cloud.terminal.route.mixin``, so this confirms the
host-console table gets that behaviour — the instance-terminal side has
the exhaustive process-liveness coverage; here we pin that the host
model resolves a dead PID to None (auto-cleaning the row) and enforces
the one-row-per-session constraint.
"""
import subprocess
import time
from contextlib import suppress

from psycopg2 import IntegrityError

from odoo.tests.common import TransactionCase


class TestCloudHostTerminalRoute(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Route = self.env['cloud.host.terminal.route']
        self.user = self.env.ref('base.user_admin')

    def _live_process(self):
        proc = subprocess.Popen(
            ['python3', '-c', 'import time; time.sleep(30)'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._kill_quiet, proc)
        return proc

    @staticmethod
    def _kill_quiet(proc):
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            proc.wait(timeout=2)

    def test_inherits_the_shared_routing_mixin(self):
        self.assertIn('cloud.terminal.route.mixin', self.Route._inherit)

    def test_resolve_returns_the_row_when_the_pid_is_alive(self):
        proc = self._live_process()
        route = self.Route.sudo().create({
            'session_id': 'host-alive',
            'pid': proc.pid,
            'port': 23456,
            'auth_token': 'tok',
            'user_id': self.user.id,
        })
        self.assertEqual(self.Route._resolve('host-alive'), route)

    def test_resolve_cleans_a_dead_row_and_returns_none(self):
        proc = subprocess.Popen(
            ['python3', '-c', 'pass'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=2)
        self.Route.sudo().create({
            'session_id': 'host-dead',
            'pid': proc.pid,
            'port': 23457,
            'auth_token': 'tok',
            'user_id': self.user.id,
        })
        time.sleep(0.1)
        self.assertIsNone(self.Route._resolve('host-dead'))
        self.assertFalse(
            self.Route.sudo().search([('session_id', '=', 'host-dead')]),
        )

    def test_session_id_is_unique(self):
        proc = self._live_process()
        self.Route.sudo().create({
            'session_id': 'host-dup', 'pid': proc.pid, 'port': 1,
            'auth_token': 'tok', 'user_id': self.user.id,
        })
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self.Route.sudo().create({
                'session_id': 'host-dup', 'pid': proc.pid, 'port': 2,
                'auth_token': 'tok', 'user_id': self.user.id,
            })

    def test_a_gc_cron_sweeps_this_table(self):
        """Stale host-shell routes must be collected by a background sweep.

        The rows carry an encrypted bearer token for the most privileged
        capability in the system (a login shell on the whole host), so
        relying on the lazy cleanup inside ``_resolve`` is not enough: a
        session whose subprocess dies uncleanly and is never revisited
        would linger forever. The instance terminal's table is swept on a
        cron; this pins the same guarantee here.

        ``cron.active`` is deliberately not asserted: ``deploy-update``
        pauses our crons before running this suite, which would make the
        assertion unpassable inside a deploy.
        """
        cron = self.env.ref(
            'incubacloud.cron_cloud_host_terminal_route_gc',
        )
        self.assertEqual(cron.model_id.model, 'cloud.host.terminal.route')
        self.assertIn('_gc()', cron.code)
