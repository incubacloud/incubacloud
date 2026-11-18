"""A module update leaves the crons' schedule as it was.

A cron record outside ``noupdate`` is written again on every update, and
one that declares ``nextcall`` gets that date back: the nightly prune's,
``2025-01-01 03:00``, is in the past, so every update ran it within the
minute, on every server, as if it were 03:00 (18-nov-2026). The date of a
cron's first run is declared once, under ``noupdate``.
"""
import os
from datetime import timedelta

from lxml import etree

from odoo import fields
from odoo.modules.module import get_manifest, get_module_path
from odoo.tests.common import TransactionCase
from odoo.tools import file_open
from odoo.tools.convert import convert_file


class TestUpdateKeepsTheCronSchedule(TransactionCase):

    def _core_modules(self):
        """Return the installed modules of this repository.

        :rtype: list[str]
        """
        root = os.path.dirname(os.path.realpath(get_module_path("incubacloud")))
        installed = self.env["ir.module.module"].search([
            ("state", "in", ("installed", "to upgrade")),
        ]).mapped("name")
        return sorted(
            name for name in installed
            if (path := get_module_path(name, display_warning=False))
            and os.path.dirname(os.path.realpath(path)) == root
        )

    def test_an_update_leaves_the_next_prune_where_it_was(self):
        """Loading the prune's cron again, as an update does, keeps its date."""
        cron = self.env.ref("incubacloud.cron_docker_prune")
        planned = fields.Datetime.now().replace(microsecond=0) + timedelta(hours=7)
        cron.nextcall = planned
        convert_file(
            self.env, "incubacloud", "data/docker_prune_cron.xml", {},
            mode="update",
        )
        self.env.invalidate_all()
        self.assertEqual(cron.nextcall, planned)

    def test_no_updatable_cron_sets_its_next_run(self):
        """No cron of this repository has ``nextcall`` written by an update."""
        offending = []
        for module in self._core_modules():
            for path in get_manifest(module).get("data", ()):
                if not path.endswith(".xml"):
                    continue
                with file_open(f"{module}/{path}", "rb") as handle:
                    root = etree.parse(handle).getroot()
                for record in root.iter("record"):
                    if record.get("model") != "ir.cron" or any(
                        node.get("noupdate") in ("1", "True", "true")
                        for node in record.iterancestors()
                    ):
                        continue
                    if record.find("field[@name='nextcall']") is not None:
                        offending.append(f"{module}/{path}: {record.get('id')}")
        self.assertFalse(
            offending,
            "an update sets these crons' next run again; declare it under "
            "noupdate: %s" % ", ".join(offending),
        )
