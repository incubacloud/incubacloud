"""Moving an instance to another project leaves the rows keyed on it alone.

An instance's name is unique within its project. Declared as the table
constraint ``UNIQUE (project_id, name)``, its index was one PostgreSQL
counts as a key a foreign key can use, so a write of ``project_id`` was a
change of the instance's key: it took the row ``FOR UPDATE``, and any
REPEATABLE READ transaction begun before that write committed which then
stored a row pointing at the instance (a job, a log) failed with ``could
not serialize access due to concurrent update``. queue_job runs every job
in such a transaction. A spare panel claimed by an account is moved to the
account's project in that way (8-nov-2026).

The same rule is now a unique index with a predicate, ``WHERE project_id
IS NOT NULL``, which every row the old constraint compared satisfies (it
never compared two rows without a project: NULLs are distinct). PostgreSQL
does not count a partial index as a key (its documentation of explicit
locking, "FOR UPDATE"), so the move takes ``FOR NO KEY UPDATE`` and the
older transaction stores its row.

The transactions here are real, as in ``test_move_own_job_row``: the
fixture is committed (and removed after), and each side runs on a cursor
of its own.
"""
from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import SUPERUSER_ID, api
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

_INDEX = "cloud_instance_name_project_uniq"


class TestInstanceNameIndex(TransactionCase):
    """The name's index: its shape, what it refuses, what it lets through."""

    def setUp(self):
        """A project holding one instance, in the test's transaction."""
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Name Index"})
        self.instance = self.env["cloud.instance"].create({
            "name": "name-index",
            "project_id": self.project.id,
            "environment": "staging",
        })
        self.env.flush_all()

    def test_the_rule_is_a_partial_unique_index_and_no_constraint(self):
        """Unique, over ``(project_id, name)``, with a predicate; no constraint.

        A constraint of that name left behind (a base updated without the
        migration of 1.0.228) would keep the key the move must not change.
        """
        self.env.cr.execute(
            """
            SELECT i.indisunique, i.indpred IS NOT NULL,
                   pg_get_indexdef(i.indexrelid)
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indexrelid
             WHERE c.relname = %s
            """,
            (_INDEX,),
        )
        row = self.env.cr.fetchone()
        self.assertTrue(row, "the index of an instance's name is missing")
        unique, partial, definition = row
        self.assertTrue(unique)
        self.assertTrue(partial, definition)
        self.assertIn("(project_id, name)", definition)
        self.env.cr.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s", (_INDEX,),
        )
        self.assertFalse(self.env.cr.fetchone())

    def test_two_instances_of_one_name_in_a_project_are_refused(self):
        """The guarantee the constraint gave, kept by the index."""
        with mute_logger("odoo.sql_db"), \
                self.assertRaises(UniqueViolation), \
                self.env.cr.savepoint():
            self.env["cloud.instance"].create({
                "name": "name-index",
                "project_id": self.project.id,
                "environment": "staging",
            })
            self.env.flush_all()

    def test_the_name_of_an_archived_instance_is_refused_too(self):
        """The index has no ``active`` in it: an archived namesake still holds it.

        Below the friendlier refusal of ``create``, which a write of the
        name does not pass through.
        """
        self.env.cr.execute(
            "UPDATE cloud_instance SET active = false WHERE id = %s",
            (self.instance.id,),
        )
        other = self.env["cloud.instance"].create({
            "name": "name-index-2",
            "project_id": self.project.id,
            "environment": "staging",
        })
        self.env.flush_all()
        with mute_logger("odoo.sql_db"), \
                self.assertRaises(UniqueViolation), \
                self.env.cr.savepoint():
            self.env.cr.execute(
                "UPDATE cloud_instance SET name ="
                " (SELECT name FROM cloud_instance WHERE id = %s) WHERE id = %s",
                (self.instance.id, other.id),
            )

    def test_one_name_in_two_projects_is_accepted(self):
        """Unique within a project, not across them."""
        other = self.env["cloud.project"].create({"name": "Name Index 2"})
        twin = self.env["cloud.instance"].create({
            "name": "name-index",
            "project_id": other.id,
            "environment": "staging",
        })
        self.env.flush_all()
        self.assertTrue(twin.id)


class TestInstanceMoveUnderRealTransactions(TransactionCase):
    """A job stored on an instance another transaction moved meanwhile."""

    def setUp(self):
        """Commit a host, two projects and an instance in the first; remove them after."""
        super().setUp()
        self.registry = self.env.registry
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            host = env["cloud.host"].create({
                "name": "name-index-host", "ip_address": "192.0.2.95",
                "port": 22, "user": "root", "login_type": "ssh_key",
                "wildcard_domain": "name-index.example.com",
                "status": "compatible",
            })
            projects = env["cloud.project"].create([
                {"name": "Name Index From"}, {"name": "Name Index To"},
            ])
            instance = env["cloud.instance"].create({
                "name": "name-index-moved", "project_id": projects[0].id,
                "host_id": host.id, "environment": "production",
            })
            self.host_id = host.id
            self.project_ids = tuple(projects.ids)
            self.instance_id = instance.id
            self.job_type_id = env["cloud.job.type"].search(
                [("code", "=", "host_probe")], limit=1,
            ).id
            cr.commit()
        self.addCleanup(self._remove_fixture)

    def _remove_fixture(self):
        """Delete the committed rows, children first."""
        with self.registry.cursor() as cr:
            instances = (self.instance_id,)
            cr.execute(
                "SELECT id FROM cloud_job WHERE host_id = %s OR instance_id IN %s",
                (self.host_id, instances),
            )
            job_ids = tuple(r[0] for r in cr.fetchall()) or (0,)
            for table in ("cloud_job_log_chunk", "cloud_audit_log", "cloud_alert"):
                cr.execute(f"DELETE FROM {table} WHERE job_id IN %s", (job_ids,))
            cr.execute("DELETE FROM cloud_job WHERE id IN %s", (job_ids,))
            cr.execute(
                "DELETE FROM mail_message WHERE (model = 'cloud.instance'"
                " AND res_id IN %s) OR (model = 'cloud.project' AND res_id IN %s)",
                (instances, self.project_ids),
            )
            for table in ("cloud_instance_domain", "cloud_alert", "cloud_audit_log"):
                cr.execute(
                    f"DELETE FROM {table} WHERE instance_id IN %s", (instances,),
                )
            cr.execute("DELETE FROM cloud_instance WHERE id IN %s", (instances,))
            cr.execute("DELETE FROM cloud_project WHERE id IN %s", (self.project_ids,))
            for table in ("cloud_alert", "cloud_audit_log", "cloud_host_whitelist"):
                cr.execute(f"DELETE FROM {table} WHERE host_id = %s", (self.host_id,))
            cr.execute("DELETE FROM cloud_host WHERE id = %s", (self.host_id,))
            cr.commit()

    def _job_transaction_around(self, write):
        """Take a job's snapshot, let *write* commit, then store a job on the instance.

        :param write: ``callable(cursor)`` run, and committed, in between
        :raises SerializationFailure: when the store conflicts
        """
        with self.registry.cursor() as job_cr:
            env = api.Environment(job_cr, SUPERUSER_ID, {})
            # The job's first read fixes its snapshot, as ``job.lock()`` does.
            job_cr.execute(
                "SELECT project_id FROM cloud_instance WHERE id = %s",
                (self.instance_id,),
            )
            with self.registry.cursor() as other_cr:
                write(other_cr)
            env["cloud.job"].create({
                "name": "name-index-job", "host_id": self.host_id,
                "instance_id": self.instance_id, "job_type_id": self.job_type_id,
            })
            env.flush_all()
            job_cr.commit()

    def test_the_harness_meets_the_conflict_a_key_lock_makes(self):
        """The instance held ``FOR UPDATE`` and written: the job's store fails."""

        def strong_write(cr):
            """Lock the instance as a change of its key does, and write it."""
            cr.execute(
                "SELECT id FROM cloud_instance WHERE id = %s FOR UPDATE",
                (self.instance_id,),
            )
            cr.execute(
                "UPDATE cloud_instance SET auto_rebuild = NOT auto_rebuild"
                " WHERE id = %s",
                (self.instance_id,),
            )

        with mute_logger("odoo.sql_db"), self.assertRaises(SerializationFailure):
            self._job_transaction_around(strong_write)

    def test_a_job_on_the_instance_is_stored_after_a_move(self):
        """The move of ``project_id`` is no change of key: the job is stored."""

        def move(cr):
            """Move the instance to the second project."""
            cr.execute(
                "UPDATE cloud_instance SET project_id = %s WHERE id = %s",
                (self.project_ids[1], self.instance_id),
            )

        self._job_transaction_around(move)
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT count(*) FROM cloud_job WHERE instance_id = %s"
                " AND name = 'name-index-job'",
                (self.instance_id,),
            )
            self.assertEqual(cr.fetchone()[0], 1)
            cr.execute(
                "SELECT project_id FROM cloud_instance WHERE id = %s",
                (self.instance_id,),
            )
            self.assertEqual(cr.fetchone()[0], self.project_ids[1])
