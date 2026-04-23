"""Tests for ``cloud.terminal.route``.

Covers the liveness check, the auto-GC path, and that ``_resolve``
returns ``None`` (not raising) when the row's PID is dead.
"""
import os
import signal
import subprocess
import time
from contextlib import suppress

from odoo.tests.common import TransactionCase


class TestCloudTerminalRouteLiveness(TransactionCase):

    def _user(self):
        return self.env.ref('base.user_admin')

    def setUp(self):
        super().setUp()
        self.Route = self.env['cloud.terminal.route']

    def _make_dummy_process(self):
        """Spawn a trivial long-running subprocess whose PID we
        can inject into a route row. ``sleep infinity`` is not
        portable; use Python's own stdlib so the test runs in any
        container."""
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

    def test_resolve_returns_row_when_pid_is_alive(self):
        proc = self._make_dummy_process()
        route = self.Route.sudo().create({
            'session_id': 'alive-sid',
            'pid': proc.pid,
            'port': 12345,
            'auth_token': 'tok',
            'user_id': self._user().id,
        })
        found = self.Route._resolve('alive-sid')
        self.assertTrue(found)
        self.assertEqual(found.id, route.id)

    def test_resolve_returns_none_and_cleans_row_when_pid_is_dead(self):
        # Spawn then immediately kill → a PID that is guaranteed
        # to be dead by the time we query.
        proc = subprocess.Popen(
            ['python3', '-c', 'pass'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=2)
        self.Route.sudo().create({
            'session_id': 'dead-sid',
            'pid': proc.pid,
            'port': 12346,
            'auth_token': 'tok',
            'user_id': self._user().id,
        })
        # Give the OS a tick to reap.
        time.sleep(0.1)
        found = self.Route._resolve('dead-sid')
        self.assertIsNone(found)
        # Row was auto-cleaned.
        still_there = self.Route.sudo().search(
            [('session_id', '=', 'dead-sid')],
        )
        self.assertFalse(still_there)

    def test_gc_unlinks_dead_rows_and_keeps_live_ones(self):
        live_proc = self._make_dummy_process()
        dead_proc = subprocess.Popen(
            ['python3', '-c', 'pass'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dead_proc.wait(timeout=2)
        self.Route.sudo().create([
            {
                'session_id': 'gc-live',
                'pid': live_proc.pid,
                'port': 12347,
                'auth_token': 'tok',
                'user_id': self._user().id,
            },
            {
                'session_id': 'gc-dead',
                'pid': dead_proc.pid,
                'port': 12348,
                'auth_token': 'tok',
                'user_id': self._user().id,
            },
        ])
        time.sleep(0.1)
        self.Route._gc()
        remaining = self.Route.sudo().search(
            [('session_id', 'in', ['gc-live', 'gc-dead'])],
        ).mapped('session_id')
        self.assertEqual(remaining, ['gc-live'])

    def test_session_id_is_unique(self):
        proc = self._make_dummy_process()
        self.Route.sudo().create({
            'session_id': 'dup',
            'pid': proc.pid,
            'port': 1,
            'auth_token': 'tok',
            'user_id': self._user().id,
        })
        # Second row with same session_id must fail. Wrap in a
        # savepoint so the outer test transaction stays valid.
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self.Route.sudo().create({
                'session_id': 'dup',
                'pid': proc.pid,
                'port': 2,
                'auth_token': 'tok',
                'user_id': self._user().id,
            })


class TestCloudTerminalRouteIsProcessAlive(TransactionCase):
    """Unit test the ``_is_process_alive`` helper directly."""

    def setUp(self):
        super().setUp()
        self.Route = self.env['cloud.terminal.route']

    def test_pid_zero_is_never_alive(self):
        """pid=0 is a sentinel for 'no process' — the helper must
        not interpret it as the current process group."""
        r = self.Route.sudo().create({
            'session_id': 'p0', 'pid': 0, 'port': 1, 'auth_token': 't',
            'user_id': self.env.ref('base.user_admin').id,
        })
        self.assertFalse(r._is_process_alive())

    def test_high_pid_is_not_alive(self):
        """PID 2**22 is almost certainly not assigned (above default
        kernel.pid_max on most systems)."""
        r = self.Route.sudo().create({
            'session_id': 'pmax', 'pid': 2 ** 22, 'port': 1,
            'auth_token': 't',
            'user_id': self.env.ref('base.user_admin').id,
        })
        self.assertFalse(r._is_process_alive())

    def test_current_process_is_alive(self):
        r = self.Route.sudo().create({
            'session_id': 'self', 'pid': os.getpid(), 'port': 1,
            'auth_token': 't',
            'user_id': self.env.ref('base.user_admin').id,
        })
        self.assertTrue(r._is_process_alive())
