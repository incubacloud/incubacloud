"""The metrics central's server cannot leave the fleet with the history on it.

#61, phase A. ``metrics_central_host_id`` let go of its host on its own
(``ondelete='set null'``), and nothing stopped deleting or archiving that
host, nor deploying the central on another one, which started an empty
central there and left the history behind. Three levels refuse it now:
the model, the routes and the jobs. The ways out are *Start an empty
central* on another host, asked for with a payload flag only the panel may
set, and *Retire the central*. Nothing here touches how the metrics travel.
"""
import asyncio
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.http import Request
from odoo.tests import new_test_user
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.models._central_sizing import (
    RAM_GB_ALONE,
    RAM_GB_WITH_INSTANCES,
    central_sizing,
)
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.delete_host_executor import (
    DeleteHostExecutor,
)
from odoo.addons.incubacloud.models.metrics_acl_sync_executor import (
    MetricsAclSyncExecutor,
)
from odoo.addons.incubacloud.models.observability_central_executor import (
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)


class CentralGuardCase(ObservabilityExecutorCase):
    """A central on ``self.host``, and a second server ``self.other``."""

    def setUp(self):
        super().setUp()
        self.settings.write({"metrics_central_host_id": self.host.id})
        self.other = self.env["cloud.host"].create({
            "name": "HY",
            "ip_address": "10.0.0.41",
            "user": "root",
            "wildcard_domain": "hy.example.com",
        })
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _call(self, method, **kwargs):
        """Call the controller method *method* with a request on this env."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            return getattr(self.controller, method)(**kwargs)

    def _jobs(self, code, host=None):
        """Return the jobs of type *code* on *host* (any host when None)."""
        domain = [("job_type_id.code", "=", code)]
        if host is not None:
            domain.append(("host_id", "=", host.id))
        return self.env["cloud.job"].search(domain)

    def _checks(self, cls, host, payload=None):
        """Run the pre-run checks of a *cls* job on *host*.

        :param payload: the job's payload; ``None`` leaves the spec'd
            mock's own, which is not a dict
        """
        executor = self._make(cls)
        executor.job.host_id = host
        executor.job.instance_id = self.env["cloud.instance"]
        if payload is not None:
            executor.job.payload = payload
        with patch.object(cls, "_write_system_direct", autospec=True):
            executor.pre_run_checks()


class TestTheModelRefuses(CentralGuardCase):

    def test_deleting_the_central_s_host_is_refused(self):
        with self.assertRaises(UserError) as caught:
            self.host.unlink()
        self.assertIn('"HX" runs the metrics central', str(caught.exception))
        self.assertTrue(self.host.exists())

    def test_archiving_it_is_refused(self):
        with self.assertRaises(UserError):
            self.host.write({"active": False})
        self.assertTrue(self.host.active)

    def test_its_other_fields_are_written_as_before(self):
        """The central's server is still a server: only leaving is refused."""
        self.host.write({"name": "HX renamed"})
        self.assertEqual(self.host.name, "HX renamed")

    def test_another_host_goes_as_before(self):
        self.other.write({"active": False})
        self.assertFalse(self.other.active)
        self.other.unlink()
        self.assertFalse(self.other.exists())

    def test_a_central_recorded_on_an_archived_host_protects_nothing(self):
        """Before 1.0.227 nothing stopped archiving it: there is no central."""
        self.settings.metrics_central_host_id = self.other
        self.env.cr.execute(
            "UPDATE cloud_host SET active = false WHERE id = %s", [self.other.id],
        )
        self.other.invalidate_recordset(["active"])
        self.assertFalse(self.env["cloud.settings"]._live_central_host())
        self.host.write({"active": False})
        self.assertFalse(self.host.active)

    def test_the_database_restricts_the_deletion(self):
        """The last line: the foreign key no longer lets go of the host."""
        self.env.cr.execute("""
            SELECT c.confdeltype
              FROM pg_constraint c
              JOIN pg_attribute a
                ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
             WHERE c.conrelid = 'cloud_settings'::regclass
               AND c.contype = 'f'
               AND a.attname = 'metrics_central_host_id'
        """)
        self.assertEqual(self.env.cr.fetchone()[0], "r")


class TestTheRoutesRefuse(CentralGuardCase):

    def test_delete_host_queues_nothing_for_the_central_s_host(self):
        self.host.traefik_deployed = True
        answer = self._call("cloud_delete_host", host_id=self.host.id)
        self.assertIs(answer["ok"], False)
        self.assertIn("start an empty central", answer["error"])
        self.assertFalse(self._jobs("delete_host"))
        self.assertTrue(self.host.active)

    def test_delete_host_without_a_proxy_is_refused_too(self):
        """The branch that archives or unlinks at once refuses first."""
        self.assertFalse(self.host.traefik_deployed)
        answer = self._call("cloud_delete_host", host_id=self.host.id)
        self.assertIs(answer["ok"], False)
        self.assertTrue(self.host.exists())
        self.assertTrue(self.host.active)

    def test_enable_observability_on_another_host_is_refused(self):
        answer = self._call("cloud_monitoring_deploy_central", host_id=self.other.id)
        self.assertIs(answer["ok"], False)
        self.assertIn('The central runs on "HX"', answer["error"])
        self.assertFalse(self._jobs("deploy_metrics_central"))

    def test_enable_observability_on_the_central_s_host_queues(self):
        """Redeploying where it runs keeps working, the history with it."""
        answer = self._call("cloud_monitoring_deploy_central", host_id=self.host.id)
        self.assertIs(answer["ok"], True)
        self.assertEqual(len(self._jobs("deploy_metrics_central", self.host)), 1)

    def test_enable_observability_with_no_central_queues_anywhere(self):
        self.settings.metrics_central_host_id = False
        answer = self._call("cloud_monitoring_deploy_central", host_id=self.other.id)
        self.assertIs(answer["ok"], True)
        job = self._jobs("deploy_metrics_central", self.other)
        self.assertEqual(len(job), 1)
        self.assertFalse((job.payload or {}).get("discard_history"))


class TestStartAnEmptyCentral(CentralGuardCase):

    def test_it_queues_the_deployment_with_the_flag(self):
        answer = self._call("cloud_monitoring_empty_central", host_id=self.other.id)
        self.assertIs(answer["ok"], True)
        job = self._jobs("deploy_metrics_central", self.other)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.payload, {"discard_history": True})
        self.assertTrue(self.env["cloud.audit.log"].search([
            ("action", "=", "Empty metrics central started"),
            ("details", "=", "HY (was on HX)"),
        ]))

    def test_not_on_the_central_s_own_host(self):
        answer = self._call("cloud_monitoring_empty_central", host_id=self.host.id)
        self.assertIs(answer["ok"], False)
        self.assertFalse(self._jobs("deploy_metrics_central"))

    def test_not_without_a_central(self):
        self.settings.metrics_central_host_id = False
        answer = self._call("cloud_monitoring_empty_central", host_id=self.other.id)
        self.assertIs(answer["ok"], False)
        self.assertFalse(self._jobs("deploy_metrics_central"))

    def test_the_flag_is_refused_to_anyone_but_the_platform(self):
        """``enqueue`` is reachable through ``call_kw`` and the MCP."""
        manager = new_test_user(
            self.env, login="central61-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        with self.assertRaises(AccessError):
            self.env["cloud.job"].with_user(manager).enqueue(
                self.other.id, False, "deploy_metrics_central",
                {"discard_history": True},
            )
        with self.assertRaises(AccessError):
            self.env["cloud.job"].with_user(manager).enqueue(
                self.other.id, False, "deploy_metrics_central",
                {"discard_history": False},
            )
        self.assertFalse(self._jobs("deploy_metrics_central"))

    def test_its_deployment_moves_the_panel_and_says_what_it_left(self):
        executor = self._make(ObservabilityCentralExecutor)
        executor.job.host_id = self.other
        executor.job.payload = {"discard_history": True}
        lines = []
        executor._sys = lines.append
        executor._facts = {
            "ic_central_up": "True", "ic_central_gateway": "http://172.17.0.1:8428",
        }
        executor._shipped_accounts = []
        asyncio.run(executor.on_success({}))
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_central_host_id, self.other)
        self.assertIn("The central on HX is left as it was", "\n".join(lines))
        # HX is no longer the central's: it may go.
        self.host.write({"active": False})


class TestRetireTheCentral(CentralGuardCase):

    def test_it_forgets_the_central_and_switches_observability_off(self):
        answer = self._call("cloud_monitoring_retire_central")
        self.assertEqual(answer, {"ok": True, "host": "HX"})
        self.settings.invalidate_recordset()
        self.assertFalse(self.settings.metrics_central_host_id)
        self.assertFalse(self.settings.metrics_central_url)
        self.assertFalse(self.settings.metrics_remote_write_url)
        self.assertFalse(self.settings.metrics_enabled)
        self.assertTrue(self.env["cloud.audit.log"].search([
            ("action", "=", "Metrics central retired"), ("details", "=", "HX"),
        ]))
        # The way out of the refusal: the server may go now.
        self.host.write({"active": False})
        self.assertFalse(self.host.active)

    def test_nothing_to_retire(self):
        self.settings.metrics_central_host_id = False
        answer = self._call("cloud_monitoring_retire_central")
        self.assertIs(answer["ok"], False)

    def test_not_while_a_deployment_of_it_waits(self):
        """It would record the host again when it ended."""
        self._call("cloud_monitoring_deploy_central", host_id=self.host.id)
        answer = self._call("cloud_monitoring_retire_central")
        self.assertIs(answer["ok"], False)
        self.assertEqual(self.settings.metrics_central_host_id, self.host)

    def test_only_a_manager_may(self):
        user = new_test_user(
            self.env, login="central61-user", groups="base.group_user",
        )
        with self.assertRaises(AccessError):
            self.env["cloud.settings"].with_user(user).retire_metrics_central()
        self.assertEqual(self.settings.metrics_central_host_id, self.host)


class TestTheJobsRefuse(CentralGuardCase):

    def test_delete_host_is_refused_on_the_central_s_host(self):
        """A teardown queued before the central went there, or retried."""
        with self.assertRaises(JobRefused) as caught:
            self._checks(DeleteHostExecutor, self.host)
        self.assertIn('"HX" runs the metrics central', str(caught.exception))
        self._checks(DeleteHostExecutor, self.other)

    def test_the_central_is_refused_on_another_host(self):
        with self.assertRaises(JobRefused) as caught:
            self._checks(ObservabilityCentralExecutor, self.other, payload={})
        self.assertIn('The central runs on "HX"', str(caught.exception))
        with self.assertRaises(JobRefused):
            self._checks(ObservabilityCentralExecutor, self.other)
        with self.assertRaises(JobRefused):
            self._checks(
                ObservabilityCentralExecutor, self.other,
                payload={"discard_history": "yes"},
            )

    def test_with_the_flag_it_starts_there(self):
        self._checks(
            ObservabilityCentralExecutor, self.other,
            payload={"discard_history": True},
        )

    def test_on_its_own_host_and_with_no_central_it_runs(self):
        """As in production: the central redeployed where it runs."""
        self._checks(ObservabilityCentralExecutor, self.host, payload={})
        self.settings.metrics_central_host_id = False
        self._checks(ObservabilityCentralExecutor, self.other, payload={})

    def test_the_account_sync_is_held_to_the_central_s_host(self):
        self._checks(MetricsAclSyncExecutor, self.host, payload={})
        with self.assertRaises(JobRefused):
            self._checks(MetricsAclSyncExecutor, self.other, payload={})

    def test_the_cron_still_redeploys_the_central_where_it_runs(self):
        self.settings.metrics_central_revision = 0
        job_id = self.env["cloud.host"]._reconcile_central_revision()
        self.assertTrue(job_id)
        self.assertEqual(self.env["cloud.job"].browse(job_id).host_id, self.host)
        self._checks(ObservabilityCentralExecutor, self.host, payload={})


class TestTheSizingNote(CentralGuardCase):

    def _sizing(self, **kwargs):
        values = {
            "servers": 2, "retention_days": 90, "disk_free_gb": 40.0,
            "ram_total_gb": 4.0, "ram_available_gb": 4.0, "cpu_cores": 2,
            "has_instances": False, "full_setup": True,
        }
        values.update(kwargs)
        return central_sizing(**values)

    def _codes(self, **kwargs):
        return [code for code, _values in self._sizing(**kwargs)["warnings"]]

    def test_a_host_of_its_own_that_fits(self):
        sizing = self._sizing()
        self.assertEqual(sizing["warnings"], [])
        self.assertEqual(sizing["disk_needed_gb"], 3.6)
        self.assertEqual(sizing["ram_needed_gb"], RAM_GB_ALONE)

    def test_each_figure_that_falls_short(self):
        self.assertEqual(self._codes(disk_free_gb=3.0), ["disk"])
        self.assertEqual(self._codes(ram_total_gb=2.0), ["ram"])
        self.assertEqual(self._codes(cpu_cores=1), ["cpu"])
        self.assertEqual(self._codes(full_setup=False), ["no_full_setup"])
        self.assertEqual(self._codes(servers=21, disk_free_gb=100.0), ["not_measured"])

    def test_retention_scales_the_disk_and_zero_keeps_everything(self):
        self.assertEqual(self._sizing(retention_days=180)["disk_needed_gb"], 5.2)
        sizing = self._sizing(retention_days=0)
        self.assertIsNone(sizing["disk_needed_gb"])
        self.assertEqual(self._codes(retention_days=0), ["keeps_everything"])

    def test_a_host_with_instances_needs_more_and_some_left_free(self):
        sizing = self._sizing(has_instances=True, ram_total_gb=8.0, ram_available_gb=1.0)
        self.assertEqual(sizing["ram_needed_gb"], RAM_GB_WITH_INSTANCES)
        self.assertEqual([code for code, _v in sizing["warnings"]], ["ram_free"])

    def test_a_host_never_measured_says_so_and_judges_nothing(self):
        sizing = self._sizing(disk_free_gb=0, ram_total_gb=0, cpu_cores=0, ram_available_gb=0)
        self.assertFalse(sizing["measured"])
        self.assertEqual(sizing["warnings"], [])

    def test_the_route_words_it(self):
        self.other.write({"traefik_deployed": True})
        answer = self._call("cloud_monitoring_central_sizing", host_id=self.other.id)
        self.assertIs(answer["ok"], True)
        self.assertFalse(answer["measured"])
        self.assertIn("Not measured yet", answer["notes"][0])
        self.other.write({
            "disk_free_gb": 1.0, "ram_total_gb": 4.0, "cpu_cores": 2,
        })
        answer = self._call("cloud_monitoring_central_sizing", host_id=self.other.id)
        self.assertIs(answer["fits"], False)
        self.assertEqual(len(answer["notes"]), 1)
        self.assertIn("Free disk 1.0 GB", answer["notes"][0])

    def test_the_settings_say_where_the_central_runs(self):
        data = self._call("cloud_get_general_settings")
        self.assertEqual(data["metrics_central_host"], {"id": self.host.id, "name": "HX"})
        self.settings.metrics_central_host_id = False
        self.assertIs(self._call("cloud_get_general_settings")["metrics_central_host"], False)
