"""The first steps: the marks, what writes them, the card's state and the migration.

``cloud.first.steps`` is the one source of what each step means; these pin
that a date once written stays (a server deleted takes nothing back), that
the triggers write the marks in the request that makes them true without
ever breaking it, and what the card shows to whom.
"""
import importlib.util
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tests.common import TransactionCase
from odoo.tools.misc import file_path

from odoo.addons.incubacloud.models.cloud_first_step import CloudFirstSteps

T0 = datetime(2026, 11, 1, 9, 0, 0)


def _keep_first_steps_on(case):
    """Keep the first steps on for *case*, whatever layer the database has.

    A layer may turn them off (the master's does): these tests are of
    core's marks, so ``_enabled`` is patched on the registry's own class,
    for the test only. A test that turns them off patches that class too.

    :param case: the running test case
    :return: the registry's ``cloud.first.steps`` class
    """
    cls = type(case.env["cloud.first.steps"])
    patcher = patch.object(cls, "_enabled", return_value=True)
    patcher.start()
    case.addCleanup(patcher.stop)
    return cls


class _FirstStepsBase(TransactionCase):
    """A clean slate of marks and the records the steps look at."""

    def setUp(self):
        super().setUp()
        self.StepsClass = _keep_first_steps_on(self)
        self.Steps = self.env["cloud.first.steps"]
        # What the database of the test held before: only what each test
        # creates counts.
        self.env.cr.execute("DELETE FROM cloud_first_step")
        # Only the jobs of the types a mark reads: on a copy of production
        # every job ended was hundreds of thousands of rows, rewritten in
        # each test, and the database gave out (QA of candidate S, [6c]).
        codes = tuple(sorted({
            code
            for kinds in self.Steps._job_types().values()
            for types in kinds.values()
            for code in types
        }))
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'cancelled'"
            " WHERE state IN ('done', 'failed') AND job_type_id IN"
            " (SELECT id FROM cloud_job_type WHERE code IN %s)",
            [codes],
        )
        self.env.cr.execute("DELETE FROM cloud_instance_domain")
        self.env.cr.execute("DELETE FROM cloud_instance_backup")
        self.env.cr.execute(
            "UPDATE cloud_instance SET smtp_relay_host = NULL,"
            " smtp_relay_state = 'unknown', boot_test = FALSE"
        )
        self._age("cloud_host", T0 + timedelta(days=400))
        self._age("cloud_backup_backend", T0 + timedelta(days=400))
        self._age("cloud_project", T0 + timedelta(days=400))
        self._age("cloud_instance", T0 + timedelta(days=400))
        self.env.cr.execute("UPDATE cloud_host SET traefik_deployed = FALSE")
        self.env.invalidate_all()
        self.manager = new_test_user(
            self.env, login="fs-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )

    def _age(self, table, when, ids=None):
        """Set the creation date of *table*'s rows (all, or *ids*) to *when*."""
        query = f"UPDATE {table} SET create_date = %s"  # noqa: S608 - a test's table
        params = [when]
        if ids is not None:
            query += " WHERE id IN %s"
            params.append(tuple(ids))
        self.env.cr.execute(query, params)
        self.env.invalidate_all()

    def _host(self, name="fs-host", **vals):
        """A server, created as the panel creates one."""
        return self.env["cloud.host"].create({
            "name": name, "ip_address": "10.0.12.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "fs.example.com",
        } | vals)

    def _backend(self):
        """A backup destination."""
        return self.env["cloud.backup.backend"].create({
            "name": "fs-backend", "backend_type": "s3",
            "s3_bucket": "fs-bucket", "s3_path": "backups",
            "s3_access_key_id": "AKIAFS", "s3_secret_access_key": "shh",
        })

    def _instance(self, host, state="deployed", **vals):
        """A production instance on *host*, in a project of its own."""
        project = self.env["cloud.project"].create({"name": f"fs-proj-{host.id}"})
        return self.env["cloud.instance"].create({
            "name": "fs-inst", "project_id": project.id,
            "environment": "production", "host_id": host.id, "state": state,
            "backup_backend_id": self._backend().id,
        } | vals)

    def _job(self, code, host, instance=None, state="done", payload=None):
        """A job of type *code* created in *state*, without any of its hooks."""
        return self.env["cloud.job"].create({
            "name": f"fs {code}", "host_id": host.id,
            "instance_id": instance.id if instance else False,
            "job_type_id": self.env.ref(f"incubacloud.{code}").id,
            "state": state, "payload": payload or {},
        })

    def _row(self, code):
        """The mark *code*, or an empty recordset."""
        return self.env["cloud.first.step"].search([("code", "=", code)])


@tagged("-at_install", "post_install")
class TestFirstStepMarks(_FirstStepsBase):
    """Each mark, tried and achieved by its derivation, and kept once written."""

    def test_a_server_created_and_deleted_before_anyone_looks_stays_tried(self):
        host = self._host()
        self.assertTrue(self._row("server").attempted_at)
        self.assertFalse(self._row("server").achieved_at)
        host.unlink()
        self.Steps._reconcile()
        self.assertTrue(self._row("server").attempted_at)

    def test_the_first_date_wins(self):
        self._host()
        first = self._row("server").attempted_at
        self.assertTrue(first, "written by the creation itself")
        second = self._host(name="fs-host-2", ip_address="10.0.12.2")
        self._age("cloud_host", T0 - timedelta(days=5), second.ids)
        self.Steps._reconcile()
        self.assertEqual(self._row("server").attempted_at, first)

    def test_the_server_is_achieved_by_a_full_setup_done(self):
        host = self._host()
        self._job("full_setup", host)
        self.Steps._reconcile()
        self.assertTrue(self._row("server").achieved_at)

    def test_the_failure_moves_until_achieved_then_stays(self):
        host = self._host()
        hardening = self._job("host_hardening", host, state="failed")
        self.Steps._reconcile()
        self.assertEqual(self._row("server").failed_job_type, "host_hardening")
        setup = self._job("full_setup", host, state="failed")
        self.env.cr.execute(
            "UPDATE cloud_job SET write_date = write_date + interval '1 minute'"
            " WHERE id = %s", (setup.id,),
        )
        self.env.invalidate_all()
        self.Steps._reconcile()
        self.assertEqual(self._row("server").failed_job_type, "full_setup")
        self.assertTrue(hardening.exists())
        self._job("full_setup", host)
        self.Steps._reconcile()
        row = self._row("server")
        self.assertTrue(row.achieved_at)
        self.assertEqual(row.failed_job_type, "full_setup")
        self._job("host_hardening", host, state="failed")
        self.Steps._reconcile()
        self.assertEqual(self._row("server").failed_job_type, "full_setup")

    def test_backups_tried_by_a_destination_achieved_by_a_backup(self):
        host = self._host()
        instance = self._instance(host)
        self.assertTrue(self._row("backups").attempted_at)
        self.assertFalse(self._row("backups").achieved_at)
        self.env["cloud.instance.backup"].create({
            "instance_id": instance.id, "kind": "chain",
            "backup_type": "Full", "backup_time": T0,
        })
        self.Steps._reconcile()
        self.assertEqual(self._row("backups").achieved_at, T0)

    def test_project_and_deploy(self):
        host = self._host()
        instance = self._instance(host, state="draft")
        self.assertTrue(self._row("project").achieved_at)
        deploy = self._job("deploy_instance", host, instance, state="queued")
        deploy.write({"state": "done"})
        row = self._row("deploy")
        # A database with deploys of its own (a copy of a running panel)
        # was tried before this one.
        self.assertTrue(row.attempted_at)
        self.assertLessEqual(row.attempted_at, deploy.create_date)
        self.assertTrue(row.achieved_at)

    def test_push_needs_a_push_and_the_boot_test(self):
        host = self._host()
        instance = self._instance(host, boot_test=False)
        self._job("rebuild_instance", host, instance)
        self.Steps._reconcile()
        self.assertFalse(self._row("push"))
        self._job("rebuild_instance", host, instance, payload={"trigger": "webhook"})
        self.Steps._reconcile()
        self.assertFalse(self._row("push"), "the instance does not test its image")
        instance.boot_test = True
        self.Steps._reconcile()
        self.assertTrue(self._row("push").achieved_at)

    def test_domain_own_name_then_applied_by_a_later_rebuild(self):
        host = self._host()
        instance = self._instance(host)
        Domain = self.env["cloud.instance.domain"]
        Domain.create({"instance_id": instance.id, "hostname": "fs-inst.fs.example.com"})
        self.assertFalse(self._row("domain"))
        own = Domain.create({"instance_id": instance.id, "hostname": "erp.acme.test"})
        self.assertTrue(self._row("domain").attempted_at)
        before = self._job("rebuild_instance", host, instance)
        self._age("cloud_job", own.create_date - timedelta(minutes=5), before.ids)
        self.Steps._reconcile()
        self.assertFalse(self._row("domain").achieved_at)
        self._job("rebuild_instance", host, instance)
        self.Steps._reconcile()
        self.assertTrue(self._row("domain").achieved_at)

    def test_mail_tried_by_a_relay_achieved_when_it_delivers(self):
        instance = self._instance(self._host())
        instance.smtp_relay_host = "smtp.acme.test"
        self.assertTrue(self._row("mail").attempted_at)
        self.assertFalse(self._row("mail").achieved_at)
        instance.write({"smtp_relay_state": "ok", "smtp_relay_checked_at": T0})
        self.assertEqual(self._row("mail").achieved_at, T0)

    def test_archived_records_count(self):
        host = self._host()
        self.env.cr.execute("DELETE FROM cloud_first_step")
        host.active = False
        self.Steps._reconcile()
        self.assertTrue(self._row("server").attempted_at)

    def test_an_archived_instance_whose_server_was_deleted_breaks_no_mark(self):
        host = self._host()
        instance = self._instance(host)
        self.env["cloud.instance.domain"].create({
            "instance_id": instance.id, "hostname": "fs-inst.fs.example.com",
        })
        self.env.cr.execute(
            "UPDATE cloud_instance SET active = FALSE WHERE id = %s", [instance.id],
        )
        self.env.invalidate_all()
        host.unlink()
        self.assertFalse(instance.with_context(active_test=False).host_id)
        self.env.cr.execute("DELETE FROM cloud_first_step")
        self.env.invalidate_all()
        self.assertIn("project", self.Steps._reconcile())
        self.assertTrue(self._row("project").achieved_at)
        self.assertFalse(self._row("domain"), "without its server it is not counted")

    def test_a_mark_that_fails_to_derive_leaves_the_others(self):
        host = self._host()
        self._job("full_setup", host)
        self.env.cr.execute("DELETE FROM cloud_first_step")
        self.env.invalidate_all()
        with patch.object(
            CloudFirstSteps, "_derive_backups", side_effect=RuntimeError("boom"),
        ), self.assertLogs(
            "odoo.addons.incubacloud.models.cloud_first_step", level="ERROR",
        ):
            changed = self.Steps._reconcile()
        self.assertIn("server", changed)
        self.assertNotIn("backups", changed)
        self.assertTrue(self._row("server").achieved_at)

    def test_the_job_end_writes_the_mark(self):
        host = self._host()
        job = self._job("full_setup", host, state="started")
        job.write({"state": "done"})
        self.assertTrue(self._row("server").achieved_at)

    def test_a_trigger_that_fails_breaks_neither_create_nor_the_job_end(self):
        with patch.object(
            CloudFirstSteps, "_reconcile", side_effect=RuntimeError("boom"),
        ), self.assertLogs(
            "odoo.addons.incubacloud.models.cloud_first_step", level="ERROR",
        ):
            host = self._host()
            job = self._job("full_setup", host, state="started")
            job.write({"state": "done"})
        self.assertTrue(host.exists())
        self.assertEqual(job.state, "done")
        self.assertFalse(self._row("server"))

    def test_mark_writes_a_fact_once_and_tells_the_layers(self):
        with patch.object(CloudFirstSteps, "_on_marks_changed") as heard:
            self.assertTrue(self.Steps._mark("clicked", attempted=True))
            self.assertFalse(self.Steps._mark("clicked", attempted=True))
        heard.assert_called_once_with(["clicked"])
        self.assertTrue(self._row("clicked").attempted_at)

    def test_disabled_writes_nothing(self):
        with patch.object(self.StepsClass, "_enabled", return_value=False):
            self._host()
            self.assertEqual(self.Steps._reconcile(), [])
            self.assertFalse(self.Steps._mark("clicked", attempted=True))
            self.assertEqual(self.Steps._backfill(), [])
            self.assertEqual(self.Steps._state(self.manager), {"enabled": False})
        self.assertFalse(self.env["cloud.first.step"].search([]))

    def test_reconcile_reports_what_changed_and_reads_nothing_when_complete(self):
        host = self._host()
        self._job("full_setup", host)
        self.assertEqual(self.Steps._reconcile(["server"]), ["server"])
        with patch.object(CloudFirstSteps, "_derive_server") as derive:
            self.assertEqual(self.Steps._reconcile(["server"]), [])
        derive.assert_not_called()


@tagged("-at_install", "post_install")
class TestFirstStepsState(_FirstStepsBase):
    """What the card shows: each step's state, what comes next, to whom."""

    def _state(self, user=None):
        return self.Steps.with_user(user or self.manager)._state(user or self.manager)

    def _step(self, state, code):
        return next(step for step in state["steps"] if step["code"] == code)

    def _skip_unless_new(self):
        """Skip a test of a new panel on a copy of a running one.

        New: no server at all, and no project the manager sees.
        """
        hosts = self.env["cloud.host"].sudo().with_context(active_test=False)
        projects = self.env["cloud.project"].with_user(self.manager)
        if hosts.search_count([]) or projects.search_count([]):
            self.skipTest("the database holds servers or projects: not a new panel")

    def test_a_new_panel_shows_three_steps_to_do_and_the_first_next(self):
        self._skip_unless_new()
        state = self._state()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["visible"])
        self.assertFalse(state["collapsed"])
        self.assertEqual((state["done"], state["required"]), (0, 3))
        self.assertEqual(state["next"], "server")
        self.assertEqual(
            [step["code"] for step in state["steps"]],
            ["server", "backups", "deploy", "domain", "mail"],
        )
        self.assertEqual(self._step(state, "server")["state"], "todo")
        self.assertEqual(
            self._step(state, "server")["actions"][0]["route"], "new_host",
        )
        self.assertEqual(self._step(state, "domain")["actions"], [])
        self.assertEqual(
            self._step(state, "domain")["help"], "After your first deployment.",
        )
        # Core hangs nothing on the header; a layer, its own header's data.
        self.assertEqual(CloudFirstSteps._header(self.Steps, self.manager), {})
        self.assertEqual(state["header"], self.Steps._header(self.manager))

    def test_no_button_says_new_project(self):
        """The page's header keeps «New Project», found by its name in QA."""
        labels = [
            action["label"]
            for step in self.Steps.with_user(self.manager)._steps()
            for action in step["actions"]
        ]
        self.assertTrue(labels)
        self.assertFalse([label for label in labels if "new project" in label.lower()])

    def test_the_deploy_action_follows_what_exists(self):
        self._skip_unless_new()
        state = self._state()
        self.assertEqual(self._step(state, "deploy")["actions"][0]["code"], "new_project")
        host = self._host()
        project = self.env["cloud.project"].create({"name": "fs-only"})
        self._age("cloud_project", T0 - timedelta(days=500), project.ids)
        project.member_ids |= self.manager
        state = self._state()
        deploy = self._step(state, "deploy")
        self.assertEqual(deploy["state"], "started")
        self.assertEqual(deploy["actions"][0]["code"], "new_production")
        self.assertEqual(deploy["actions"][0]["params"], {"project_id": project.id})
        instance = self.env["cloud.instance"].create({
            "name": "fs-prod", "project_id": project.id,
            "environment": "production", "host_id": host.id,
            "backup_backend_id": self._backend().id,
        })
        deploy = self._step(self._state(), "deploy")
        self.assertEqual(deploy["actions"][0]["route"], "instance_detail")
        self.assertEqual(deploy["actions"][0]["params"]["instance_id"], instance.id)

    def test_a_failed_attempt_says_so_and_leads_to_the_job(self):
        host = self._host()
        self._job("full_setup", host, state="failed")
        server = self._step(self._state(), "server")
        self.assertEqual(server["state"], "started")
        self.assertTrue(server["failed"])
        self.assertEqual(server["help"], "The last attempt failed.")
        self.assertEqual(server["actions"][0]["route"], "host_detail")
        self.assertEqual(server["actions"][0]["params"], {"host_id": host.id})

    def test_the_failure_leads_to_a_server_the_mark_counts(self):
        host = self._host()
        other = self._host(name="fs-other", ip_address="10.0.12.9")
        self._job("full_setup", host, state="failed")
        self._job("full_setup", other, state="failed")
        # On the registry's class, where a layer's own domain would win.
        with patch.object(
            self.StepsClass, "_first_step_host_domain",
            return_value=[("id", "=", host.id)],
        ):
            server = self._step(self._state(), "server")
        self.assertTrue(server["failed"])
        self.assertEqual(server["actions"][0]["params"], {"host_id": host.id})

    def test_a_try_a_layer_leaves_out_does_not_start_the_step(self):
        self._host()
        with patch.object(CloudFirstSteps, "_started", return_value=False):
            server = self._step(self._state(), "server")
        self.assertEqual(server["state"], "todo")
        self.assertTrue(self._row("server").attempted_at, "kept all the same")

    def test_required_done_collapses_then_all_done_is_gone(self):
        host = self._host()
        instance = self._instance(host, state="deployed")
        self._job("full_setup", host)
        self._job("backup_create", host, instance)
        self._job("deploy_instance", host, instance)
        state = self._state()
        self.assertEqual(state["done"], 3)
        self.assertTrue(state["visible"])
        self.assertTrue(state["collapsed"])
        self.assertEqual(state["optional_left"], 2)
        self.assertEqual(state["next"], "domain")
        self.assertEqual(
            self._step(state, "domain")["actions"][0]["params"]["tab"], "networking",
        )
        self.Steps._mark("domain", achieved=True)
        self.Steps._mark("mail", achieved=True)
        state = self._state()
        self.assertFalse(state["visible"])
        self.assertFalse(state["pending"])
        self.assertFalse(state["next"])

    def test_without_the_manager_role_there_is_no_card(self):
        consultant = new_test_user(
            self.env, login="fs-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        self.assertEqual(self._state(consultant), {"enabled": False})

    def test_closing_is_per_user_and_it_comes_back(self):
        other = new_test_user(
            self.env, login="fs-manager-2",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.Steps._set_display(self.manager, True)
        state = self._state()
        self.assertFalse(state["visible"])
        self.assertTrue(state["hidden"])
        self.assertTrue(state["pending"])
        self.assertTrue(self._state(other)["visible"])
        self.Steps._set_display(self.manager, False)
        self.assertTrue(self._state()["visible"])

    def test_a_running_panel_does_not_see_it_for_its_optional_steps(self):
        host = self._host()
        instance = self._instance(host)
        self.env["cloud.instance.backup"].create({
            "instance_id": instance.id, "kind": "chain",
            "backup_type": "Full", "backup_time": T0,
        })
        self.env.cr.execute("DELETE FROM cloud_first_step")
        host.traefik_deployed = True
        self.Steps._backfill()
        state = self._state()
        self.assertEqual(state["done"], 3)
        self.assertTrue(state["pending"])
        self.assertFalse(state["visible"])
        # Opened again from the menu, it shows while something is left.
        self.Steps._set_display(self.manager, False)
        self.assertTrue(self._state()["visible"])


@tagged("-at_install", "post_install")
class TestFirstStepsMigration(_FirstStepsBase):
    """1.0.232 fills the marks of a running panel, backfilled."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "end_first_steps_1_0_232",
            file_path("incubacloud/migrations/1.0.232/end-first-steps.py"),
        )
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)

    def test_backfills_from_the_records_when_the_jobs_are_purged(self):
        host = self._host()
        instance = self._instance(host)
        self.env["cloud.instance.backup"].create({
            "instance_id": instance.id, "kind": "chain",
            "backup_type": "Full", "backup_time": T0,
        })
        host.traefik_deployed = True
        self._age("cloud_host", T0 - timedelta(days=3), host.ids)
        self._age("cloud_instance", T0 - timedelta(days=2), instance.ids)
        self.env.cr.execute("DELETE FROM cloud_first_step")
        self.env.invalidate_all()
        self.migration.migrate(self.env.cr, "1.0.231")
        self.env.invalidate_all()
        server, deploy, backups = (
            self._row(code) for code in ("server", "deploy", "backups")
        )
        self.assertEqual(server.achieved_at, T0 - timedelta(days=3))
        self.assertEqual(deploy.achieved_at, T0 - timedelta(days=2))
        self.assertEqual(backups.achieved_at, T0)
        self.assertTrue(all(r.backfilled for r in (server, deploy, backups)))

    def test_backfill_does_not_tell_the_layers(self):
        self._host()
        self.env.cr.execute("DELETE FROM cloud_first_step")
        with patch.object(CloudFirstSteps, "_on_marks_changed") as heard:
            self.migration.migrate(self.env.cr, "1.0.231")
        heard.assert_not_called()
        self.assertTrue(self._row("server").backfilled)

    def test_an_archived_instance_without_its_server_does_not_break_it(self):
        host = self._host()
        instance = self._instance(host)
        self.env["cloud.instance.domain"].create({
            "instance_id": instance.id, "hostname": "erp.acme.test",
        })
        self.env.cr.execute(
            "UPDATE cloud_instance SET active = FALSE WHERE id = %s", [instance.id],
        )
        self.env.invalidate_all()
        host.unlink()
        self.env.cr.execute("DELETE FROM cloud_first_step")
        self.env.invalidate_all()
        self.migration.migrate(self.env.cr, "1.0.231")
        self.env.invalidate_all()
        self.assertTrue(self._row("project").backfilled)
        self.assertFalse(self._row("domain"))

    def test_a_backfill_that_fails_does_not_break_the_update(self):
        self._host()
        self.env.cr.execute("DELETE FROM cloud_first_step")
        with patch.object(
            CloudFirstSteps, "_backfill", side_effect=RuntimeError("boom"),
        ), self.assertLogs(level="ERROR"):
            self.migration.migrate(self.env.cr, "1.0.231")
        self.assertFalse(self.env["cloud.first.step"].search([]))

    def test_a_fresh_install_runs_nothing(self):
        self._host()
        self.env.cr.execute("DELETE FROM cloud_first_step")
        self.migration.migrate(self.env.cr, None)
        self.assertFalse(self.env["cloud.first.step"].search([]))


@tagged("-at_install", "post_install")
class TestFirstStepsRoutes(HttpCase):
    """The card's two calls, as the signed-in user."""

    def setUp(self):
        super().setUp()
        _keep_first_steps_on(self)
        new_test_user(
            self.env, login="fs-route-manager", password="fs-route-manager-pw",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        new_test_user(
            self.env, login="fs-route-user", password="fs-route-user-pw",
            groups="base.group_user,incubacloud.group_cloud_user",
        )

    def test_a_manager_gets_the_card_and_closes_it(self):
        self.authenticate("fs-route-manager", "fs-route-manager-pw")
        state = self.make_jsonrpc_request("/cloud/first_steps", {})
        self.assertTrue(state["enabled"])
        state = self.make_jsonrpc_request(
            "/cloud/first_steps/set_hidden", {"hidden": True},
        )
        self.assertTrue(state["hidden"])
        self.assertFalse(state["visible"])

    def test_a_user_gets_nothing(self):
        self.authenticate("fs-route-user", "fs-route-user-pw")
        self.assertEqual(
            self.make_jsonrpc_request("/cloud/first_steps", {}), {"enabled": False},
        )
        self.assertEqual(
            self.make_jsonrpc_request("/cloud/first_steps/set_hidden", {"hidden": True}),
            {"enabled": False},
        )
