"""The 1.0.198 post-migrate raises a template pin too old for 20.0.

A panel offers 20.0 only if its pinned doodba template renders it
(v10.0.3 on its card), and a panel installed with a core older than
1.0.195 (21-oct-2026) holds v9.6.1, unless someone changed it: the
field's default never reached it. The migration raises such a tag to
v10.0.8, both tags written in the script, and leaves every other pin as
it is. The database under test already ran the upgrade, so each case
writes a pin and runs the script's step on it.
"""
import importlib.util
import logging
import os
from unittest.mock import MagicMock, patch

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import _odoo_versions


def _load_migration():
    """Import the 1.0.198 post-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.198",
        "post-migrate.py",
    )
    spec = importlib.util.spec_from_file_location(
        "ic_post_migrate_1_0_198", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheTemplatePinIsRaised(TransactionCase):
    """Only a version tag below v10.0.3, 20.0's floor, moves: to v10.0.8."""

    def setUp(self):
        super().setUp()
        self.migration = _load_migration()
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.logger = MagicMock(spec=logging.Logger)
        self.startPatcher(patch.object(self.migration, "_logger", self.logger))

    def _migrate(self, pin):
        """Run the step on a panel pinned to *pin*; return the pin after it."""
        self.settings.copier_template_ref = pin
        self.migration._raise_the_template_pin(self.env)
        return self.settings.copier_template_ref or ""

    def test_a_tag_below_the_floor_is_raised(self):
        """To v10.0.8, with a log line naming both pins and the floor."""
        for pin in ("v9.6.1", "v9.7.0", "v9.10.0", "v10.0.2", "9.7.0"):
            self.logger.reset_mock()
            with self.subTest(pin=pin):
                self.assertEqual(self._migrate(pin), "v10.0.8")
                self.logger.info.assert_called_once()
                args = self.logger.info.call_args.args
                self.assertIn("raised to", args[0])
                self.assertEqual(args[1:], (pin, "v10.0.8", "v10.0.3"))

    def test_a_tag_at_or_past_the_floor_stays_and_logs_nothing(self):
        for pin in ("v10.0.3", "v10.0.8", "v11.0.0"):
            self.logger.reset_mock()
            with self.subTest(pin=pin):
                self.assertEqual(self._migrate(pin), pin)
                self.logger.info.assert_not_called()

    def test_an_empty_pin_a_branch_or_a_commit_stays_and_logs_nothing(self):
        """The template's default branch, or something chosen by hand."""
        for pin in ("", "main", "1f3e2d4c5b6a79881f3e2d4c5b6a79881f3e2d4c"):
            self.logger.reset_mock()
            with self.subTest(pin=pin):
                self.assertEqual(self._migrate(pin), pin)
                self.logger.info.assert_not_called()

    def test_it_reads_neither_the_card_nor_the_code_s_default(self):
        """A panel may upgrade from 1.0.197 after 20.0's card is gone or
        the field's default moved: the script's own tags still apply."""
        field = type(self.settings)._fields["copier_template_ref"]
        with patch.dict(_odoo_versions.VERSIONS), patch.object(
            field, "default", lambda model: "v11.0.0",
        ):
            del _odoo_versions.VERSIONS["20.0"]
            self.assertEqual(self._migrate("v9.6.1"), "v10.0.8")
