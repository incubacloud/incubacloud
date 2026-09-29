"""The 1.0.138 pre-migrate hands the host shell's records to this module.

If the identifiers stayed with the module that shipped the shell before,
the end of that module's next upgrade would delete what it no longer
declares — the ACL lines, the cron, its action. The migration re-points
them instead, and has to be safe to run twice.

The database under test already ran the upgrade, so each case first
gives a real identifier back to a stand-in owner, then runs the script.
As the load-order feedback note warns, this cannot prove the script runs
at the right moment of a real upgrade; the rehearsal on a production
copy does that. It proves what the SQL selects.
"""
import importlib.util
import os

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

_FORMER_OWNER = "x_former_host_shell_owner"


def _load_migration():
    """Import the 1.0.138 pre-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.138",
        "pre-migrate.py",
    )
    spec = importlib.util.spec_from_file_location(
        "ic_pre_migrate_1_0_138", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHostShellTakeover(TransactionCase):
    """Exact names and prefixes move; nothing else does."""

    def setUp(self):
        """Load the script and flush so raw SQL sees the ORM's writes."""
        super().setUp()
        self.migration = _load_migration()
        self.env.flush_all()

    def _give_away(self, name):
        """Hand this module's identifier *name* to the stand-in owner."""
        self.env.cr.execute(
            "UPDATE ir_model_data SET module = %s, noupdate = true "
            "WHERE module = 'incubacloud' AND name = %s",
            (_FORMER_OWNER, name),
        )
        self.assertEqual(self.env.cr.rowcount, 1, name)

    def _owner(self, name):
        """Return ``(module, noupdate)`` of the identifier *name*."""
        self.env.cr.execute(
            "SELECT module, noupdate FROM ir_model_data WHERE name = %s",
            (name,),
        )
        rows = self.env.cr.fetchall()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_named_and_prefixed_identifiers_come_back(self):
        """A model, the cron's action and a field, by name and prefix."""
        names = (
            "model_cloud_host_session",
            "cron_cloud_host_terminal_route_gc_ir_actions_server",
            "field_cloud_host_session__client_ip",
            "selection__cloud_host_session__state__open",
        )
        for name in names:
            self._give_away(name)
        self.migration.migrate(self.env.cr, "1.0.137")
        for name in names:
            self.assertEqual(self._owner(name), ("incubacloud", False), name)

    def test_an_unrelated_identifier_stays_put(self):
        """A field of another model is not swept along by a prefix."""
        self._give_away("field_cloud_host__name")
        self.migration.migrate(self.env.cr, "1.0.137")
        self.assertEqual(self._owner("field_cloud_host__name")[0], _FORMER_OWNER)

    def test_a_second_run_changes_nothing(self):
        """With every identifier already here, the script is a no-op."""
        self.migration.migrate(self.env.cr, "1.0.137")
        self.env.cr.execute(
            "SELECT count(*) FROM ir_model_data "
            "WHERE module = 'incubacloud' AND name = 'model_cloud_host_session'",
        )
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_the_foreign_keys_come_back_too(self):
        """Reflected constraints of the two tables are re-pointed."""
        self.env.cr.execute(
            """
            UPDATE ir_model_constraint c
               SET module = (SELECT id FROM ir_module_module
                              WHERE name = 'base')
              FROM ir_model m
             WHERE m.id = c.model AND m.model = 'cloud.host.session'
               AND c.name = 'cloud_host_session_host_id_fkey'
            """,
        )
        self.assertEqual(self.env.cr.rowcount, 1)
        self.migration.migrate(self.env.cr, "1.0.137")
        self.env.cr.execute(
            """
            SELECT mm.name FROM ir_model_constraint c
              JOIN ir_module_module mm ON mm.id = c.module
             WHERE c.name = 'cloud_host_session_host_id_fkey'
            """,
        )
        self.assertEqual(self.env.cr.fetchall(), [("incubacloud",)])
