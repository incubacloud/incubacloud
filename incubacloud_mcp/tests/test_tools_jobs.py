"""The buttons that queue jobs, as tools: the panel's roles, and what asks."""

import base64
import json
from unittest.mock import patch

import asyncssh

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from .common import ConfirmMixin, McpHttpMixin, McpUsersMixin, PanelFixtureMixin


def _job_state(_tools, result):
    """Stand in for ``_with_job_state``: the answer as it is."""
    return result


@tagged("post_install", "-at_install")
class TestJobTools(PanelFixtureMixin, McpUsersMixin, TransactionCase):
    """What each tool asks, who is refused before, and what it queues."""

    def setUp(self):
        """A manager, a consultant and a developer of the project, and an
        outsider consultant."""
        super().setUp()
        self._panel_fixture()
        self.manager = self._cloud_user("mcp-jobs-mgr", group="group_cloud_manager")
        self.consultant = self._cloud_user(
            "mcp-jobs-consultant", group="group_cloud_consultant"
        )
        self.developer = self._cloud_user("mcp-jobs-dev")
        self.outsider = self._cloud_user(
            "mcp-jobs-outsider", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.consultant.id), (4, self.developer.id)]
        Job = type(self.env["cloud.job"])
        self.enqueue = self.startPatcher(
            patch.object(Job, "enqueue", autospec=True, return_value=4242)
        )
        self.startPatcher(
            patch.object(
                type(self.env["incubacloud.mcp.tools"]),
                "_with_job_state",
                autospec=True,
                side_effect=_job_state,
            )
        )

    def _tools(self, user):
        """The tools' model as *user*."""
        return self.env["incubacloud.mcp.tools"].with_user(user)

    def _ask(self, user, name, args):
        """Return ``(prepared args, what user is asked)`` for tool *name*."""
        tools = self._tools(user)
        prepare = getattr(tools, f"_prepare_{name}", None)
        if prepare:
            args = prepare(args)
        spec = self.env["incubacloud.mcp.catalog"]._spec(name)
        return args, tools._confirmation(spec, args)

    def _job(self, code, state, instance=None, host=None):
        """Create a job of type *code* in *state*."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        return (
            self.env["cloud.job"]
            .sudo()
            .create(
                {
                    "name": code,
                    "instance_id": instance.id if instance else False,
                    "host_id": (host or self.host).id,
                    "job_type_id": job_type.id,
                    "state": state,
                }
            )
        )

    # ── run_instance_action ───────────────────────────────────────────────

    def test_stopping_a_staging_asks_nothing_and_queues_its_stop(self):
        """A member consultant stops a staging as the button does."""
        args, message = self._ask(
            self.consultant,
            "run_instance_action",
            {"instance_id": self.staging.id, "action": "stop"},
        )
        self.assertIsNone(message)
        answer = self._tools(self.consultant)._tool_run_instance_action(args)
        self.assertEqual(answer, {"ok": True, "job_id": 4242})
        self.enqueue.assert_called_once_with(
            self.env["cloud.job"], self.host.id, self.staging.id, "stop_instance"
        )

    def test_stopping_or_restarting_production_asks(self):
        """Production's stop and restart name it; its start asks nothing."""
        for action, word in (("stop", "Stop"), ("restart", "Restart")):
            _args, message = self._ask(
                self.consultant,
                "run_instance_action",
                {"instance_id": self.production.id, "action": action},
            )
            self.assertIn(f'{word} the production instance "mcp-acme-prod"', message)
        _args, message = self._ask(
            self.consultant,
            "run_instance_action",
            {"instance_id": self.production.id, "action": "start"},
        )
        self.assertIsNone(message)

    def test_an_action_the_instance_does_not_offer_is_refused(self):
        """Only start, stop, restart or an offered action's code."""
        with self.assertRaises(UserError):
            self._ask(
                self.consultant,
                "run_instance_action",
                {"instance_id": self.staging.id, "action": "delete_instance"},
            )

    def test_an_instance_out_of_reach_is_refused_before_being_asked(self):
        """A consultant who is no member cannot read the instance."""
        with self.assertRaises(AccessError):
            self._ask(
                self.outsider,
                "run_instance_action",
                {"instance_id": self.staging.id, "action": "stop"},
            )

    # ── export_instance ───────────────────────────────────────────────────

    def test_export_queues_the_button_s_job_and_its_file_is_downloadable(self):
        """A developer exports; the export's file is a download of its own."""
        answer = self._tools(self.developer)._tool_export_instance(
            {"instance_id": self.staging.id}
        )
        self.assertEqual(answer["job_id"], 4242)
        self.assertIn("download_backup", answer["next_step"])
        self.enqueue.assert_called_once_with(
            self.env["cloud.job"], self.host.id, self.staging.id, "export_instance"
        )
        job = self._job("export_instance", "done", instance=self.staging)
        self.env["ir.attachment"].sudo().create(
            {
                "name": "mcp-acme-staging-export.tar.gz",
                "raw": b"TAR",
                "res_model": "cloud.job",
                "res_id": job.id,
            }
        )
        _args, message = self._ask(
            self.developer, "download_backup", {"job_id": job.id}
        )
        self.assertIn("mcp-acme-staging-export.tar.gz", message)

    # ── run_host_action ───────────────────────────────────────────────────

    def test_a_check_asks_nothing_and_the_rest_asks(self):
        """Check runs at once; Full Setup and the offered actions ask, with
        the action's own warning."""
        args, message = self._ask(
            self.manager, "run_host_action", {"host_id": self.host.id, "action": "check"}
        )
        self.assertIsNone(message)
        self._tools(self.manager)._tool_run_host_action(args)
        self.enqueue.assert_called_once_with(
            self.env["cloud.job"], self.host.id, False, "host_probe"
        )
        _args, message = self._ask(
            self.manager,
            "run_host_action",
            {"host_id": self.host.id, "action": "full_setup"},
        )
        self.assertIn('full setup of the server "mcp-host" (10.0.0.90)', message)
        offered = self.host._offered_actions()
        if "push_trusted_proxies" in offered.mapped("code"):
            _args, message = self._ask(
                self.manager,
                "run_host_action",
                {"host_id": self.host.id, "action": "push_trusted_proxies"},
            )
            self.assertIn("Traefik will restart", message)

    def test_hardening_a_server_asks_as_its_job_does(self):
        """Applying the hardening asks with what the job changes on the
        server; checking what it would change asks nothing."""
        _args, message = self._ask(
            self.manager, "harden_host", {"host_id": self.host.id}
        )
        self.assertIsNone(message)
        _args, message = self._ask(
            self.manager, "harden_host", {"host_id": self.host.id, "apply": True}
        )
        self.assertIn('"Harden Host" on the server "mcp-host" (10.0.0.90)', message)
        self.assertIn("disables root SSH", message)
        self.assertNotIn("full setup", message)

    def test_a_full_setup_after_the_hardening_is_in_its_approval(self):
        """The full setup it chains asks as run_host_action's does."""
        _args, message = self._ask(
            self.manager,
            "harden_host",
            {"host_id": self.host.id, "apply": True, "chain_full_setup": True},
        )
        self.assertIn('"Harden Host" on the server "mcp-host"', message)
        self.assertIn('full setup of the server "mcp-host" (10.0.0.90)', message)
        self.assertIn("unreachable for a while", message)

    def test_a_server_action_is_a_manager_s(self):
        """Below manager, refused before anything is asked."""
        with self.assertRaises(AccessError):
            self._ask(
                self.developer,
                "run_host_action",
                {"host_id": self.host.id, "action": "check"},
            )

    def test_an_action_the_server_does_not_offer_is_refused(self):
        """A code no page offers is refused by name."""
        with self.assertRaises(UserError):
            self._ask(
                self.manager,
                "run_host_action",
                {"host_id": self.host.id, "action": "delete_host"},
            )

    # ── cancel_job, retry_job ─────────────────────────────────────────────

    def test_cancelling_names_the_job_and_refuses_one_that_ended(self):
        """The approval names the job; one that is over is refused before."""
        job = self._job("deploy_instance", "queued", instance=self.staging)
        _args, message = self._ask(self.consultant, "cancel_job", {"job_id": job.id})
        self.assertIn(f"#{job.id}", message)
        self.assertIn("mcp-acme-staging", message)
        done = self._job("deploy_instance", "done", instance=self.staging)
        with self.assertRaises(UserError):
            self._ask(self.consultant, "cancel_job", {"job_id": done.id})

    def test_cancelling_a_job_of_a_higher_role_is_refused_before_being_asked(self):
        """A consultant may not end a manager's job they can read."""
        job = self._job("move_cutover", "queued", instance=self.staging)
        with self.assertRaises(UserError):
            self._ask(self.consultant, "cancel_job", {"job_id": job.id})

    def test_a_cancel_ends_a_queued_job(self):
        """Approved, the job is cancelled as the panel's Cancel does."""
        job = self._job("deploy_instance", "queued", instance=self.staging)
        answer = self._tools(self.consultant)._tool_cancel_job({"job_id": job.id})
        self.assertEqual(answer["state"], "cancelled")

    def test_retrying_asks_only_for_what_destroys(self):
        """A deploy retries at once; a restore asks; a job that did not fail
        is refused before."""
        deploy = self._job("deploy_instance", "failed", instance=self.staging)
        _args, message = self._ask(self.consultant, "retry_job", {"job_id": deploy.id})
        self.assertIsNone(message)
        restore = self._job("restore_instance", "failed", instance=self.staging)
        _args, message = self._ask(self.developer, "retry_job", {"job_id": restore.id})
        self.assertIn(f"#{restore.id}", message)
        done = self._job("deploy_instance", "done", instance=self.staging)
        with self.assertRaises(UserError):
            self._ask(self.consultant, "retry_job", {"job_id": done.id})

    def test_retrying_a_project_s_delete_asks(self):
        """It removes the project again, as the first run would have."""
        delete = self._job("delete_project", "failed", instance=self.staging)
        _args, message = self._ask(self.manager, "retry_job", {"job_id": delete.id})
        self.assertIn(f"#{delete.id}", message)

    def test_retrying_a_job_of_a_higher_role_is_refused(self):
        """A consultant may not retry a developer's restore."""
        restore = self._job("restore_instance", "failed", instance=self.staging)
        with self.assertRaises(AccessError):
            self._ask(self.consultant, "retry_job", {"job_id": restore.id})

    # ── update_notification_preferences ───────────────────────────────────

    def test_a_preference_that_sends_notifications_elsewhere_asks(self):
        """Another Telegram chat, the emails off or every project muted ask;
        the same chat, a level or one project of several do not."""
        self.consultant.sudo().write(
            {"cloud_telegram_chat_id": "1111", "cloud_email_enabled": True}
        )
        other = self.env["cloud.project"].create({"name": "mcp-jobs-other"})
        other.member_ids = [(4, self.consultant.id)]
        name = "update_notification_preferences"
        for args, asked in (
            ({"telegram_chat_id": "2222"}, '"2222"'),
            ({"email_enabled": False}, "emails"),
            ({"muted_project_ids": [self.project.id, other.id]}, "every project"),
        ):
            with self.subTest(args=args):
                _args, message = self._ask(self.consultant, name, args)
                self.assertIn(asked, message or "")
        for args in (
            {"telegram_chat_id": " 1111 "},
            {"email_enabled": True},
            {"notification_level": "all"},
            {"muted_project_ids": [self.project.id]},
            {"action": "detect_telegram_chat"},
        ):
            with self.subTest(args=args):
                self.assertIsNone(self._ask(self.consultant, name, args)[1])

    def test_silencing_everything_or_emptying_the_chat_says_so(self):
        """Level none and an empty chat get their own text; level none already
        set, and an empty chat when none was set, do not ask."""
        name = "update_notification_preferences"
        self.consultant.sudo().write(
            {"cloud_telegram_chat_id": "1111", "cloud_notification_level": "all"}
        )
        _args, message = self._ask(self.consultant, name, {"notification_level": "none"})
        self.assertIn("Stop every notification", message or "")
        _args, message = self._ask(self.consultant, name, {"telegram_chat_id": " "})
        self.assertIn("Stop your Telegram notifications", message or "")
        self.assertNotIn("(now", message or "")
        self.consultant.sudo().write(
            {"cloud_telegram_chat_id": False, "cloud_notification_level": "none"}
        )
        for args in ({"notification_level": "none"}, {"telegram_chat_id": ""}):
            with self.subTest(args=args):
                self.assertIsNone(self._ask(self.consultant, name, args)[1])

    # ── list_jobs ─────────────────────────────────────────────────────────

    def test_jobs_are_listed_as_the_user_may_see_them(self):
        """A member sees the instance's jobs; an outsider sees none of them."""
        job = self._job("deploy_instance", "done", instance=self.staging)
        page = self._tools(self.consultant)._tool_list_jobs(
            {"instance_id": self.staging.id}
        )
        self.assertIn(job.id, [row["id"] for row in page["items"]])
        page = self._tools(self.outsider)._tool_list_jobs({"category": "all"})
        self.assertNotIn(job.id, [row["id"] for row in page["items"]])

    def test_a_cancelled_job_is_listed_stopping_while_its_queue_job_runs(self):
        """list_jobs reads stopping=true for a cancelled job whose queue
        job still runs, and false for one that ended."""
        qjob = (
            self.env["queue.job"]
            .sudo()
            .create(
                {
                    "uuid": "mcp-list-stopping",
                    "name": "stopping",
                    "state": "started",
                    "method_name": "execute",
                    "model_name": "cloud.job",
                    "func_string": "execute()",
                }
            )
        )
        stopping = self._job("deploy_instance", "pending", instance=self.staging)
        stopping.sudo().with_context(ic_skip_state_hook=True).write(
            {"queue_job_uuid": qjob.uuid, "state": "cancelled"}
        )
        ended = self._job("deploy_instance", "cancelled", instance=self.staging)
        self.env.flush_all()
        page = self._tools(self.consultant)._tool_list_jobs(
            {"instance_id": self.staging.id}
        )
        rows = {row["id"]: row for row in page["items"]}
        self.assertIs(rows[stopping.id]["stopping"], True)
        self.assertIs(rows[ended.id]["stopping"], False)

    def test_a_job_deferred_by_the_build_lock_is_listed_with_its_wait(self):
        """list_jobs reads build_wait for a job another build on its server
        defers, naming that build, and none for the others."""
        from odoo.addons.incubacloud.models.host_build_lock import (
            BUILD_LOCK_WAIT_MARKER,
        )

        holder = self._job("deploy_instance", "started", instance=self.staging)
        qjob = (
            self.env["queue.job"]
            .sudo()
            .create(
                {
                    "uuid": "mcp-list-build-wait",
                    "name": "build wait",
                    "state": "pending",
                    "result": f"{BUILD_LOCK_WAIT_MARKER} (job #{holder.id});"
                    " deferring.",
                    "method_name": "execute",
                    "model_name": "cloud.job",
                    "func_string": "execute()",
                }
            )
        )
        waiting = self._job("deploy_instance", "pending", instance=self.staging)
        waiting.sudo().with_context(ic_skip_state_hook=True).write(
            {"queue_job_uuid": qjob.uuid}
        )
        self.env.flush_all()
        page = self._tools(self.consultant)._tool_list_jobs(
            {"instance_id": self.staging.id}
        )
        rows = {row["id"]: row for row in page["items"]}
        self.assertEqual(rows[waiting.id]["build_wait"]["job_id"], holder.id)
        self.assertIsNone(rows[holder.id]["build_wait"])

    # ── clean_frozen_copy ─────────────────────────────────────────────────

    def test_cleaning_a_frozen_copy_names_its_server_and_is_a_manager_s(self):
        """No frozen copy is refused; a developer is refused; the approval
        names the old server."""
        with self.assertRaises(UserError):
            self._ask(self.manager, "clean_frozen_copy", {"instance_id": self.staging.id})
        self.staging.sudo().move_frozen_source_host_id = self.host
        with self.assertRaises(AccessError):
            self._ask(
                self.developer, "clean_frozen_copy", {"instance_id": self.staging.id}
            )
        _args, message = self._ask(
            self.manager, "clean_frozen_copy", {"instance_id": self.staging.id}
        )
        self.assertIn('old server "mcp-host"', message)

    # ── delete_instance keep_in_panel ─────────────────────────────────────

    def test_keeping_in_the_panel_says_so_and_is_sent(self):
        """Keep in panel says the instance stays; the job carries it; one
        never deployed is refused before."""
        with self.assertRaises(UserError):
            self._ask(
                self.manager,
                "delete_instance",
                {"instance_id": self.staging.id, "keep_in_panel": True},
            )
        self.staging.sudo().with_context(cloud_state_transition=True).write(
            {"state": "deployed"}
        )
        _args, message = self._ask(
            self.manager,
            "delete_instance",
            {"instance_id": self.staging.id, "keep_in_panel": True},
        )
        self.assertIn("stays in the panel as a draft", message)
        self._tools(self.manager)._tool_delete_instance(
            {"instance_id": self.staging.id, "keep_in_panel": True}
        )
        self.enqueue.assert_called_once_with(
            self.env["cloud.job"],
            self.host.id,
            self.staging.id,
            "delete_instance",
            {"keep_in_panel": True},
        )


@tagged("post_install", "-at_install")
class TestJobToolsOverHttp(
    PanelFixtureMixin, McpUsersMixin, McpHttpMixin, ConfirmMixin, HttpCase
):
    """The roles reach the agent, and the reads answer what the forms need."""

    def setUp(self):
        """The fixture and a consultant's agent."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.consultant = self._cloud_user(
            "mcp-jobs-http-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.consultant.id)]
        self.key = self._api_key(self.consultant)

    def test_a_consultant_sees_neither_server_buttons_nor_users_nor_exports(self):
        """Tools above the role are neither listed nor run."""
        for name, args in (
            ("run_host_action", {"host_id": self.host.id, "action": "check"}),
            ("clean_frozen_copy", {"instance_id": self.staging.id}),
            ("find_users", {"search": "a"}),
            ("export_instance", {"instance_id": self.staging.id}),
        ):
            with self.subTest(tool=name):
                self._assert_refused_by_role(self.key, name, args)

    def test_the_overview_lists_languages_and_backup_destinations(self):
        """What odoo_initial_lang and backup_backend_id take, by name."""
        payload = self._payload(self._call_tool(self.key, "panel_overview"))
        self.assertIn("en_US", [lang["code"] for lang in payload["languages"]])
        self.assertIn(
            {"id": self.backend.id, "name": "mcp-s3"}, payload["backup_destinations"]
        )

    def test_notification_preferences_keep_what_is_not_given(self):
        """Muting a project keeps the level and the mode."""
        self.consultant.sudo().write(
            {"cloud_notification_level": "all", "cloud_notification_mode": "daily_digest"}
        )
        result = self._confirmed_call(
            self.key,
            "update_notification_preferences",
            {"muted_project_ids": [self.project.id]},
        )
        self.assertFalse(result["isError"], result)
        prefs = self._payload(
            self._call_tool(self.key, "get_notification_preferences")
        )
        self.assertEqual(prefs["cloud_notification_level"], "all")
        self.assertEqual(prefs["cloud_notification_mode"], "daily_digest")
        self.assertEqual(
            [project["id"] for project in prefs["cloud_muted_projects"]],
            [self.project.id],
        )

    def test_get_host_leaves_the_traefik_files_out_unless_asked(self):
        """A manager's get_host has no Traefik files, but when asked; and the
        job the server runs."""
        manager = self._cloud_user("mcp-jobs-http-mgr", group="group_cloud_manager")
        key = self._api_key(manager)
        self.host.sudo().traefik_yml = "entryPoints: {}"
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1
        )
        job = (
            self.env["cloud.job"]
            .sudo()
            .create(
                {
                    "name": "probe",
                    "host_id": self.host.id,
                    "job_type_id": job_type.id,
                    "state": "started",
                }
            )
        )
        host = self._payload(self._call_tool(key, "get_host", {"host_id": self.host.id}))
        self.assertNotIn("traefik_yml", host)
        self.assertEqual(host["active_job"]["job_id"], job.id)
        self.assertEqual(host["active_job"]["job_type"], "host_probe")
        host = self._payload(
            self._call_tool(
                key, "get_host", {"host_id": self.host.id, "include_traefik_files": True}
            )
        )
        self.assertEqual(host["traefik_yml"], "entryPoints: {}")

    def test_get_host_answers_the_public_half_of_its_key(self):
        """A manager's get_host carries the key's public half, never the private."""
        key = asyncssh.generate_private_key("ssh-ed25519")
        pem = key.export_private_key("openssh")
        self.host.sudo().write(
            {"login_type": "ssh_key", "key_file": base64.b64encode(pem).decode()}
        )
        manager = self._cloud_user("mcp-jobs-key-mgr", group="group_cloud_manager")
        key_id = self._api_key(manager)
        host = self._payload(
            self._call_tool(key_id, "get_host", {"host_id": self.host.id})
        )
        public = " ".join(key.export_public_key("openssh").decode().split()[:2])
        self.assertEqual(host["ssh_public_key"], f"{public} incubacloud")
        self.assertNotIn("PRIVATE KEY", json.dumps(host))
        self.assertNotIn(base64.b64encode(pem).decode(), json.dumps(host))
