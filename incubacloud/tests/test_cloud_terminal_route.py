"""Tests for ``cloud.terminal.route``.

Covers the liveness check, the auto-GC path, and that ``_resolve``
returns ``None`` (not raising) when the row's PID is dead.

The GC half also pins *which* signal each side is allowed to trust. The
controller may ask the kernel about a PID because it spawned the process;
the cron may not, because it runs in the job-runner container where that
PID belongs to somebody else entirely.
"""
import os
import signal
import subprocess
import time
from contextlib import suppress
from datetime import timedelta

from odoo import fields
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

    def _stale(self, route):
        """Backdate *route* past the GC's TTL."""
        route.sudo().write({
            'last_seen': fields.Datetime.now() - timedelta(
                seconds=self.Route._ROUTE_TTL_SECONDS + 60,
            ),
        })

    def test_gc_keeps_recently_seen_rows_and_reaps_the_rest(self):
        fresh, stale = self.Route.sudo().create([
            {
                'session_id': 'gc-fresh',
                'pid': 0,
                'port': 12347,
                'auth_token': 'tok',
                'user_id': self._user().id,
            },
            {
                'session_id': 'gc-stale',
                'pid': 0,
                'port': 12348,
                'auth_token': 'tok',
                'user_id': self._user().id,
            },
        ])
        self._stale(stale)
        self.Route._gc()
        remaining = self.Route.sudo().search(
            [('session_id', 'in', ['gc-fresh', 'gc-stale'])],
        ).mapped('session_id')
        self.assertEqual(remaining, ['gc-fresh'])
        # ``pid`` is 0 on both — a dead PID by ``_is_process_alive``'s own
        # reckoning. The fresh row surviving is the proof the cron no
        # longer consults it.
        self.assertFalse(fresh._is_process_alive())

    def test_gc_reaps_a_stale_row_whose_pid_is_alive(self):
        """The regression this whole change exists to prevent.

        Crons run in the job-runner container; terminals are spawned by
        the web one. A PID from over there either does not exist here —
        and a working terminal gets dropped — or it collides with an
        unrelated live process here, and a dead route is kept forever.
        Both containers start their processes in the same low range, so
        the collision is the common case.

        A live PID must therefore not save a row the controller has not
        confirmed in a long time.
        """
        proc = self._make_dummy_process()
        route = self.Route.sudo().create({
            'session_id': 'gc-live-pid-stale-row',
            'pid': proc.pid,
            'port': 12349,
            'auth_token': 'tok',
            'user_id': self._user().id,
        })
        self.assertTrue(
            route._is_process_alive(),
            'fixture is wrong: the PID must be alive for this to prove '
            'anything',
        )
        self._stale(route)
        self.Route._gc()
        self.assertFalse(route.exists())

    def test_resolving_a_route_refreshes_last_seen(self):
        """``_resolve`` is the heartbeat: it runs on the web worker, on
        every proxy call, which is the only place a PID means anything."""
        proc = self._make_dummy_process()
        route = self.Route.sudo().create({
            'session_id': 'heartbeat',
            'pid': proc.pid,
            'port': 12350,
            'auth_token': 'tok',
            'user_id': self._user().id,
        })
        self._stale(route)
        before = route.last_seen
        self.assertTrue(self.Route._resolve('heartbeat'))
        self.assertGreater(
            route.last_seen, before,
            'a resolved route must be kept alive for the GC in the other '
            'container',
        )

    def test_last_seen_is_not_rewritten_on_every_poll(self):
        """The frontend polls about once a second per open terminal."""
        proc = self._make_dummy_process()
        route = self.Route.sudo().create({
            'session_id': 'throttle',
            'pid': proc.pid,
            'port': 12351,
            'auth_token': 'tok',
            'user_id': self._user().id,
        })
        self.Route._resolve('throttle')
        first = route.last_seen
        self.Route._resolve('throttle')
        self.assertEqual(route.last_seen, first)

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
