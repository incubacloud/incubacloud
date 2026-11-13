"""«Test the new image before applying it» (#91, decided on 6-nov-2026).

The boot test has a switch of its own on the instance, on by default and
independent of «Auto-Update Modules on Rebuild». A rebuild that put its
image into service without it says so first in its result and leaves a
``boot_test_skipped`` warning on the instance; the next one whose boot
test passed resolves it.
"""
import asyncio

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    BOOT_TEST_SKIPPED_ALERT,
    RebuildInstanceExecutor,
)

_BOOT = "Test new image (safe boot check)"
_UPDATE = "Update changed modules"


class TestBootTestSwitch(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.job_type = env["cloud.job.type"].search(
            [("code", "=", "rebuild_instance")], limit=1,
        ) or env["cloud.job.type"].create(
            {"name": "rebuild", "code": "rebuild_instance", "apply_to": "instance"},
        )
        self.host = env["cloud.host"].create({
            "name": "boot-switch-host", "ip_address": "192.0.2.91",
            "user": "ubuntu", "wildcard_domain": "bs.example.com",
        })
        project = env["cloud.project"].create({"name": "Boot Switch Proj"})
        self.inst = env["cloud.instance"].create({
            "name": "bootswitch", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
        })

    def _rebuilt(self, results):
        """Run a rebuild's ``on_success`` with *results*; return its log.

        :param dict results: the steps that ran, by label
        :return: the job's log lines, in order
        :rtype: list
        """
        job = self.env["cloud.job"].create({
            "host_id": self.host.id, "instance_id": self.inst.id,
            "job_type_id": self.job_type.id, "name": "Rebuild",
        })
        executor = RebuildInstanceExecutor(job, self.host)
        asyncio.run(executor.on_success(results))
        return [line for line, _source in executor._log_buffer]

    def _alert(self):
        """Return the instance's open ``boot_test_skipped`` alert."""
        return self.env["cloud.alert"].search([
            ("code", "=", BOOT_TEST_SKIPPED_ALERT),
            ("instance_id", "=", self.inst.id),
            ("state", "=", "active"),
        ])

    def test_it_is_on_by_default(self):
        self.assertTrue(self.inst.boot_test)
        self.assertTrue(self.inst.auto_update)

    def test_an_image_applied_untested_says_so_first_and_warns(self):
        log = self._rebuilt({_UPDATE: {"exit_status": 0, "stdout": ""}})
        ended = [line for line in log if line.startswith(("⚠", "✓"))]
        self.assertIn("without the boot test", ended[0])
        self.assertTrue(ended[0].startswith("⚠"))
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "warning")
        self.assertIn("«Test the new image before applying it» is off", alert.message)

    def test_a_boot_test_that_passed_resolves_the_warning(self):
        self._rebuilt({_UPDATE: {"exit_status": 0, "stdout": ""}})
        self.assertTrue(self._alert())
        log = self._rebuilt({
            _BOOT: {"exit_status": 0, "stdout": ""},
            _UPDATE: {"exit_status": 0, "stdout": ""},
        })
        self.assertFalse(self._alert())
        self.assertFalse(any("without the boot test" in line for line in log))

    def test_a_rebuild_without_its_update_says_nothing_of_it(self):
        """``auto_update`` off runs neither step: nothing new to say."""
        log = self._rebuilt({})
        self.assertFalse(self._alert())
        self.assertFalse(any("without the boot test" in line for line in log))
