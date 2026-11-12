"""The metrics central without Grafana (F3 of the observability plan).

The deployment removes what Grafana left only after the boundary answered,
never failing the job for it, unless ``incubacloud.metrics_keep_grafana_data``
exists. Every panel records its central's revision, and the reconciliation
deploys the central again while it is behind, with a growing wait after a
failure. The update empties Grafana's secrets and queues the deployment.
"""
import asyncio
import importlib.util
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

import yaml

from odoo import fields
from odoo.http import Request
from odoo.modules.module import get_module_path

from odoo.addons.incubacloud.controllers._data_load import _routes_ops
from odoo.addons.incubacloud.models import cloud_host_observability
from odoo.addons.incubacloud.models.observability_central_executor import (
    _KEEP_GRAFANA_PARAM,
    CENTRAL_REVISION,
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)
from odoo.addons.incubacloud.tests.test_observability_wiring import (
    _CENTRAL_PLAYBOOK,
)


def _tasks():
    """Return the central playbook's top-level tasks."""
    return yaml.safe_load(_CENTRAL_PLAYBOOK.read_text())[0]["tasks"]


class TestThePurge(ObservabilityExecutorCase):
    """What Grafana left goes after the boundary answers, never failing the job."""

    def test_it_comes_after_the_boundary_and_cannot_fail_the_job(self):
        names = [t.get("name") for t in _tasks()]
        purge = names.index("Remove what Grafana left")
        self.assertGreater(
            purge, names.index("Apply the access-control config and verify the boundary"),
        )
        task = _tasks()[purge]
        self.assertEqual(task["when"], "ic_purge_grafana | bool")
        self.assertTrue(task["rescue"])
        commands = [
            t["ansible.builtin.command"]["cmd"] for t in task["block"]
            if "ansible.builtin.command" in t
        ]
        self.assertIn("docker volume rm incubacloud-central_grafana-data", commands)

    def test_the_parameter_keeps_it(self):
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertIs(extra["ic_purge_grafana"], True)
        self.env["ir.config_parameter"].sudo().set_param(_KEEP_GRAFANA_PARAM, "1")
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertIs(extra["ic_purge_grafana"], False)

    def test_nothing_of_grafana_reaches_the_playbook(self):
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertFalse([key for key in extra if "grafana" in key and key != "ic_purge_grafana"])
        self.assertNotIn("ic_accounts", extra)
        self.assertNotIn("/gadmin/", extra["ic_vmauth_config"])

    def _deployed(self, **facts):
        executor = self._make(ObservabilityCentralExecutor)
        lines = []
        executor._sys = lines.append
        executor._facts = {"ic_central_up": "True",
                           "ic_central_gateway": "http://172.17.0.1:8428"} | facts
        executor._shipped_accounts = []
        asyncio.run(executor.on_success({}))
        self.settings.invalidate_recordset()
        return "\n".join(lines)

    def test_a_good_deployment_records_its_revision(self):
        self.settings.metrics_central_revision = 0
        self._deployed(ic_grafana_purge="removed")
        self.assertEqual(self.settings.metrics_central_revision, CENTRAL_REVISION)

    def test_a_purge_that_failed_is_said_not_raised(self):
        log = self._deployed(ic_grafana_purge="failed: Remove Grafana's volume: in use")
        self.assertIn("could not be removed", log)
        self.assertEqual(self.settings.metrics_central_revision, CENTRAL_REVISION)


class TestTheCentralConverges(ObservabilityExecutorCase):
    """A2: a central behind the panel's revision is deployed again."""

    def setUp(self):
        super().setUp()
        self.settings.write({
            "metrics_central_host_id": self.host.id,
            "metrics_central_revision": 0,
        })

    def _deployments(self):
        return self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "deploy_metrics_central"),
        ])

    def test_behind_it_is_queued_once(self):
        Host = self.env["cloud.host"]
        self.assertTrue(Host._reconcile_central_revision())
        self.assertEqual(len(self._deployments()), 1)
        self.assertFalse(Host._reconcile_central_revision())
        self.assertEqual(len(self._deployments()), 1)

    def test_the_cron_runs_it(self):
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertEqual(len(self._deployments()), 1)

    def test_up_to_date_nothing(self):
        self.settings.metrics_central_revision = CENTRAL_REVISION
        self.assertFalse(self.env["cloud.host"]._reconcile_central_revision())

    def test_observability_off_nothing(self):
        """The deployment would switch it on: the operator decides."""
        self.settings.metrics_enabled = False
        self.assertFalse(self.env["cloud.host"]._reconcile_central_revision())

    def test_an_archived_central_host_nothing(self):
        self.settings.metrics_central_host_id = False
        self.assertFalse(self.env["cloud.host"]._reconcile_central_revision())

    def test_after_a_failure_it_waits_then_tries_again(self):
        job_id = self.env["cloud.host"]._reconcile_central_revision()
        job = self.env["cloud.job"].browse(job_id)
        job.write({"state": "failed"})
        soon = job.write_date + timedelta(minutes=5)
        with patch.object(fields.Datetime, "now", return_value=soon):
            self.assertFalse(self.env["cloud.host"]._reconcile_central_revision())
        later = job.write_date + timedelta(
            minutes=cloud_host_observability._BACKOFF_MINUTES[1] + 1,
        )
        with patch.object(fields.Datetime, "now", return_value=later):
            self.assertTrue(self.env["cloud.host"]._reconcile_central_revision())


def _load_migration():
    """Import 1.0.229's end-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.229", "end-migrate.py",
    )
    spec = importlib.util.spec_from_file_location("ic_end_1_0_229_grafana", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheUpdateRetiresGrafana(ObservabilityExecutorCase):
    """The update empties Grafana's secrets and queues the central."""

    def setUp(self):
        super().setUp()
        self.settings.write({
            "metrics_central_host_id": self.host.id,
            "metrics_central_revision": 0,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE cloud_settings ADD COLUMN IF NOT EXISTS grafana_admin_password varchar"
        )
        self.env.cr.execute(
            "UPDATE cloud_settings SET grafana_admin_password = 'old-secret'"
        )

    def test_the_secret_is_emptied_and_the_central_queued(self):
        _load_migration().migrate(self.env.cr, "1.0.228")
        self.env.cr.execute("SELECT grafana_admin_password FROM cloud_settings")
        self.assertEqual({row[0] for row in self.env.cr.fetchall()}, {None})
        self.assertTrue(self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "deploy_metrics_central"),
        ]))

    def test_a_refused_queue_does_not_stop_the_update(self):
        with patch.object(
            type(self.env["cloud.host"]), "_reconcile_central_revision",
            side_effect=RuntimeError("busy"),
        ):
            _load_migration().migrate(self.env.cr, "1.0.228")
        self.env.cr.execute("SELECT grafana_admin_password FROM cloud_settings")
        self.assertEqual({row[0] for row in self.env.cr.fetchall()}, {None})


class TestTheMonitoringConfig(ObservabilityExecutorCase):
    """What the Monitoring page and ``get_monitoring`` read."""

    def test_the_hosts_their_agents_and_their_logs(self):
        controller = _routes_ops.OpsMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake = MagicMock(spec=Request)
        fake.env = self.env
        self.host.write({"security_logs_enabled": False})
        with patch.object(_routes_ops, "request", fake):
            data = controller.cloud_monitoring_config()
        self.assertEqual(set(data), {"ok", "enabled", "central", "hosts"})
        self.assertIs(data["central"], True)
        row = next(h for h in data["hosts"] if h["id"] == self.host.id)
        self.assertEqual(
            row,
            {"id": self.host.id, "name": "HX", "metrics_agents_state": "never",
             "security_logs_enabled": False},
        )
