"""A step of a move ends ``done``: its hook never writes its own row aside.

queue_job runs a job in a REPEATABLE READ transaction whose snapshot is
taken before the executor runs, and stores the job ``done`` in that same
transaction once it returns. The executor's hooks run on a cursor of
their own that commits first (``AbstractExecutor._durable_env``). A hook
that wrote the job's own ``cloud_job`` row there made the store of
``done`` fail with ``could not serialize access due to concurrent
update``, and queue_job ran the step again in 5 s without counting the
attempt: the move's fingerprint did so for ever, with the panel stopped.

Every other test of the move calls its hooks inside the test's own
transaction, where both cursors are one and nothing can conflict. Here
the transactions are real: a fixture is committed (and removed after),
the job's transaction takes its snapshot, the outcome is dispatched as
``AbstractExecutor.run`` dispatches it, and the job is stored done as
queue_job stores it.
"""
import asyncio

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from ..models.move_steps_executor import FINGERPRINT_LABEL
from ..models.registry import executor_registry

_FINGERPRINT = '{"tables": {"res_users": 3, "ir_attachment": 9}, "files": 12}\n'


class TestMoveStepsUnderRealTransactions(TransactionCase):
    """The fingerprint, run on committed rows by two real cursors.

    The quiet check writes nothing on its job any more (``TestQuietCheck``
    in ``test_move_chain_v2``); the fingerprint is the step that keeps a
    value there, for the restore.
    """

    def setUp(self):
        """Commit a host and a fingerprint job; remove them after the test."""
        super().setUp()
        self.registry = self.env.registry
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            host = env["cloud.host"].create({
                "name": "own-row-host", "ip_address": "192.0.2.91", "port": 22,
                "user": "root", "login_type": "ssh_key",
                "wildcard_domain": "own-row-host.example.com",
                "status": "compatible",
            })
            Type = env["cloud.job.type"]
            self.job_ids = {
                code: env["cloud.job"].create({
                    "name": code, "host_id": host.id, "state": "started",
                    "job_type_id": Type.search([("code", "=", code)], limit=1).id,
                }).id
                for code in ("move_fingerprint",)
            }
            self.host_id = host.id
            cr.commit()
        self.addCleanup(self._remove_fixture)

    def _remove_fixture(self):
        """Delete the committed rows, children first."""
        with self.registry.cursor() as cr:
            ids = tuple(self.job_ids.values())
            cr.execute("DELETE FROM cloud_job_log_chunk WHERE job_id IN %s", (ids,))
            cr.execute("DELETE FROM cloud_audit_log WHERE job_id IN %s", (ids,))
            cr.execute("DELETE FROM cloud_job WHERE id IN %s", (ids,))
            cr.execute(
                "DELETE FROM cloud_audit_log WHERE host_id = %s", (self.host_id,),
            )
            cr.execute(
                "DELETE FROM cloud_host_whitelist WHERE host_id = %s",
                (self.host_id,),
            )
            cr.execute("DELETE FROM cloud_host WHERE id = %s", (self.host_id,))
            cr.commit()

    def _run_as_queue_job(self, code, results, hook=None):
        """Dispatch *code*'s outcome as the runner does, then store it done.

        The job's transaction reads its row first, which fixes its
        snapshot as ``job.lock()`` does; the outcome runs through
        ``_dispatch_outcome`` with the durable cursor a real run gets;
        then ``cloud.job.execute``'s ``env.clear()`` and the store of
        ``done``, flushed and committed.

        :param str code: the job type, one of the fixture's
        :param dict results: what the commands printed
        :param hook: replaces the executor's ``on_success``, when given
        :raises SerializationFailure: when the store of ``done`` conflicts
        """
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = env["cloud.job"].browse(self.job_ids[code])
            cr.execute("SELECT state FROM cloud_job WHERE id = %s", (job.id,))
            executor = executor_registry.get(code)(job, job.host_id)
            if hook:
                executor.on_success = hook.__get__(executor)
            asyncio.run(executor._dispatch_outcome(results))
            env.clear()
            job.write({"state": "done"})
            env.flush_all()
            cr.commit()

    def _stored(self, code):
        """Return ``(state, result)`` of *code*'s job, as committed."""
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT state, result FROM cloud_job WHERE id = %s",
                (self.job_ids[code],),
            )
            return cr.fetchone()

    def test_the_harness_meets_the_conflict_a_hook_writing_its_row_aside_makes(self):
        """What the fingerprint did before: its row written on the hook's cursor."""

        async def writes_its_row_aside(executor, results):
            """Write the job's own row on the hook's durable cursor."""
            executor.job.sudo().result = {"files": 1, "tables": {}}

        with mute_logger("odoo.sql_db"), self.assertRaises(SerializationFailure):
            self._run_as_queue_job(
                "move_fingerprint", {}, hook=writes_its_row_aside,
            )

    def test_the_fingerprint_ends_done_with_its_count(self):
        """Stored with ``done``, for the restore to read once committed."""
        results = {FINGERPRINT_LABEL: {"stdout": _FINGERPRINT, "exit_status": 0}}
        self._run_as_queue_job("move_fingerprint", results)
        state, result = self._stored("move_fingerprint")
        self.assertEqual(state, "done")
        self.assertIn('"files": 12', result)
        self.assertIn('"res_users": 3', result)
