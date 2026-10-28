"""The 1.0.200 pre-migrate stops the upgrade on a shared remote folder.

The unique index over ``lower(remote_folder)`` cannot be built on a
database that holds a duplicate, and Odoo only warns about that. The
script looks first and raises, naming the projects. The database under
test already has the index, so each case drops it inside the test's
transaction (DDL is transactional), inserts the duplicate and runs the
script.
"""
import importlib.util
import os

from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase


def _load_migration():
    """Import the 1.0.200 pre-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.200",
        "pre-migrate.py",
    )
    spec = importlib.util.spec_from_file_location(
        "ic_pre_migrate_1_0_200", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSharedRemoteFolderStopsTheUpgrade(TransactionCase):
    """A shared folder stops the upgrade; distinct or empty ones do not."""

    def setUp(self):
        super().setUp()
        self.migration = _load_migration()
        self.env.cr.execute(
            "DROP INDEX IF EXISTS cloud_project_remote_folder_uniq"
        )

    def _project(self, name, folder):
        """Create a project, then set its folder straight in the database."""
        project = self.env["cloud.project"].create(
            {"name": name, "remote_folder": False},
        )
        self.env.cr.execute(
            "UPDATE cloud_project SET remote_folder = %s WHERE id = %s",
            [folder, project.id],
        )
        return project

    def test_a_shared_folder_stops_the_upgrade_and_names_the_projects(self):
        """Folders differing only in case are one, and both projects are named."""
        first = self._project("Mig first", "mig-shared")
        second = self._project("Mig second", "MIG-Shared")
        with self.assertRaises(UserError) as caught:
            self.migration.migrate(self.env.cr, "1.0.198")
        message = str(caught.exception)
        self.assertIn("'mig-shared'", message)
        self.assertIn("Mig first (id %s)" % first.id, message)
        self.assertIn("Mig second (id %s)" % second.id, message)

    def test_distinct_or_empty_folders_let_it_through(self):
        """No duplicate, however many projects have no folder: nothing raised."""
        self._project("Mig one", "mig-one")
        self._project("Mig two", "mig-two")
        self._project("Mig none", None)
        self._project("Mig none too", None)
        self._project("Mig blank", "")
        self._project("Mig blank too", "")
        self.assertIsNone(self.migration.migrate(self.env.cr, "1.0.198"))

    def test_a_fresh_install_is_not_checked(self):
        """Without a previous version there is nothing to look at."""
        self._project("Mig first", "mig-shared")
        self._project("Mig second", "mig-shared")
        self.assertIsNone(self.migration.migrate(self.env.cr, False))
