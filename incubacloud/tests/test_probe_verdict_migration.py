"""The update to 1.0.220 checks again the hosts the old Check may have left stuck.

Until 1.0.220 a Check (``host_probe``) that found under 10 GB free failed
its job, raised ``job_failed`` and marked the host ``unsupported``. From
1.0.220 that is a verdict (``degraded`` with ``host_low_disk``), but a
host left so before the update stays there until someone runs a Check:
the update queues one on every active ``unsupported`` host, its failure
open or dismissed. And from 1.0.220 a missing-tools alert dismissed by
hand keeps a host ``degraded`` until a Check finds the tools: one is
queued too on each active host whose newest such alert was dismissed so.
"""
import importlib.util
import os

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.tests._direct import make_host

_VERSION = "1.0.220"
_SCRIPT = "end-migrate.py"


def _load_migration():
    """Import this version's end-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", _VERSION, _SCRIPT,
    )
    spec = importlib.util.spec_from_file_location("ic_end_probe_verdict", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheUpdateChecksStuckHostsAgain(TransactionCase):

    def setUp(self):
        """Hosts with and without a failed Check open, in each status."""
        super().setUp()
        self.stuck = self._failed_check(
            make_host(self.env, "pvm-stuck", "46.4.0.80", status="unsupported"),
        )
        self.archived = self._failed_check(
            make_host(self.env, "pvm-old", "46.4.0.81", status="unsupported"),
        )
        self.archived.active = False
        self.healthy = self._failed_check(
            make_host(self.env, "pvm-ok", "46.4.0.82", status="compatible"),
        )
        self.other_failure = make_host(
            self.env, "pvm-other", "46.4.0.83", status="unsupported",
        )
        self._failed_check(self.other_failure, job_type="full_setup")
        self.dismissed = self._failed_check(
            make_host(self.env, "pvm-gone", "46.4.0.84", status="unsupported"),
        )
        self.env["cloud.alert"].search([
            ("host_id", "=", self.dismissed.id),
        ]).write({"state": "dismissed", "resolution": "manual"})
        self.tools_dismissed = make_host(
            self.env, "pvm-tools", "46.4.0.85", status="compatible",
        )
        self._tools_alert(self.tools_dismissed, "manual")
        self.tools_resolved = make_host(
            self.env, "pvm-tools-ok", "46.4.0.86", status="compatible",
        )
        self._tools_alert(self.tools_resolved, "manual")
        self._tools_alert(self.tools_resolved, "auto")

    def _failed_check(self, host, job_type="host_probe"):
        """Leave on *host* the open ``job_failed`` of a failed *job_type*.

        :return: the host.
        """
        self.env["cloud.alert"].raise_alert(
            "job_failed", "Insufficient disk: 7.3 GB free (10 GB required)",
            level="critical", host=host, key=f"job_type:{job_type}",
        )
        return host

    def _tools_alert(self, host, resolution):
        """File a missing-tools alert on *host*, closed with *resolution*."""
        self.env["cloud.alert"].raise_alert(
            "missing_deps", "Missing deployment tools: copier.", host=host,
        ).write({"state": "dismissed", "resolution": resolution})

    def _checks(self, hosts):
        """Return the Checks queued on *hosts*."""
        return self.env["cloud.job"].search([
            ("host_id", "in", hosts.ids),
            ("job_type_id.code", "=", "host_probe"),
        ])

    def test_one_check_per_host_that_may_be_stuck(self):
        """Every active unsupported host, its failure open or dismissed,
        and the one whose newest missing-tools alert a user dismissed."""
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, "1.0.219")
        self.env.invalidate_all()
        expected = (
            self.stuck | self.other_failure | self.dismissed
            | self.tools_dismissed
        )
        for host in expected:
            with self.subTest(host=host.name):
                self.assertEqual(len(self._checks(host)), 1)
        self.assertFalse(self._checks(
            self.archived | self.healthy | self.tools_resolved,
        ))

    def test_a_fresh_install_queues_nothing(self):
        """No version to upgrade from: nothing was left stuck."""
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, None)
        self.env.invalidate_all()
        self.assertFalse(self._checks(self.stuck))
