"""Auto-approval: one credential of one person runs without asking, until a date.

What a rule lets through, what it never covers, how it ends, who may grant
it, and the marks it leaves in the audit and on the jobs (issue #8).
"""

import dataclasses
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase, new_test_user

from odoo.addons.incubacloud_mcp.models import mcp_confirm
from odoo.addons.incubacloud_mcp.models.mcp_auto_approval import (
    _INTERNAL,
    MAX_DAYS,
    NOTICE_VERSION,
    SEEN_EVERY,
    SEEN_KEPT,
)
from odoo.addons.incubacloud_mcp.protocol.context import (
    APPROVAL_CONTEXT_KEY,
    VIA_CONTEXT_KEY,
    CallContext,
    Principal,
)
from odoo.addons.incubacloud_mcp.protocol.versions import MODERN

from .common import (
    FORM_CAPABILITIES,
    AutoApprovalAllowedMixin,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)

#: What a listing says of a tool that runs without asking.
AUTO_LISTED = "An auto-approval rule lets this credential run it without asking"


class AutoApprovalMixin(AutoApprovalAllowedMixin):
    """Grant rules as an Administrator, to credentials that connected, on a
    panel that allows them."""

    def _seen(self, user, credential, label="API key test"):
        """Record that *credential* of *user* connected, as ``/mcp`` does."""
        self.env["incubacloud.mcp.credential"]._seen(
            Principal(uid=user.id, credential=label, client=credential), "test-agent"
        )

    def _key_id(self, key):
        """Return the id of the API key *key*, as a rule names it."""
        _user, _name, key_id = self.env["incubacloud.mcp.auth"]._user_for_api_key(key)
        return f"API key #{key_id}"

    def _delete_key(self, credential):
        """Delete the API key named *credential* (``API key #<id>``), as
        Odoo's own removal does."""
        key_id = int(credential.removeprefix("API key #"))
        self.env.cr.execute("DELETE FROM res_users_apikeys WHERE id = %s", (key_id,))

    def _grant(self, user, credential, days=7, admin=None, **kwargs):
        """Grant a rule to *credential* of *user* as *admin* (the test's)."""
        values = {
            "days": days,
            "acknowledged": True,
            "notice_version": NOTICE_VERSION,
        } | kwargs
        return (
            self.env["incubacloud.mcp.auto.approval"]
            .with_user(admin or self.admin)
            ._grant(user.id, credential, **values)
        )


@tagged("post_install", "-at_install")
class TestAutoApprovalCalls(
    AutoApprovalMixin,
    ConfirmMixin,
    PanelFixtureMixin,
    McpUsersMixin,
    McpHttpMixin,
    HttpCase,
):
    """Calls through ``/mcp`` with and without a rule."""

    def setUp(self):
        """A manager with an API key that connected, and an Administrator."""
        super().setUp()
        self._resource_here()
        self.manager = self._cloud_user("aa-mgr", group="group_cloud_manager")
        self.admin = self._cloud_user("aa-admin", group="group_cloud_manager")
        self.key = self._api_key(self.manager, name="automation")
        self.credential = self._key_id(self.key)
        self._tool_names(self.key)

    def _project(self, name="aa-doomed"):
        """An empty project to delete."""
        return self.env["cloud.project"].create(
            {"name": name, "remote_folder": name.replace("-", "")}
        )

    def _row(self, project):
        """The audit row of the agent's ``delete_project`` of *project*."""
        return self.env["cloud.audit.log"].search(
            [
                ("action", "=", "Agent: delete_project"),
                ("details", "like", f'"project_id": {project.id}'),
            ],
            limit=1,
        )

    def test_the_connection_records_the_credential(self):
        """The key that listed the tools is one a rule may be granted to."""
        credentials = self.env["incubacloud.mcp.auto.approval"]._candidates_for(
            self.manager
        )
        self.assertEqual(
            [(item["credential"], item["kind"], item["label"]) for item in credentials],
            [(self.credential, "api_key", "API key automation")],
        )

    def test_with_a_rule_a_destructive_call_runs_without_asking(self):
        """Granted, the call runs at once; its audit row says auto and the rule."""
        rule = self._grant(self.manager, self.credential)
        project = self._project()
        result = self._call_tool(
            self.key, "delete_project", {"project_id": project.id}, FORM_CAPABILITIES
        )
        self.assertNotEqual(result.get("resultType"), "input_required", result)
        self.assertFalse(result["isError"], result)
        self.assertFalse(project.exists())
        row = self._row(project)
        self.assertEqual((row.approval, row.approval_rule_id), ("auto", rule))
        self.assertEqual(row._format()[0]["approval"], "auto")
        rule.invalidate_recordset()
        self.assertEqual(rule.use_count, 1)
        self.assertTrue(rule.last_used_at)

    def test_without_a_rule_it_asks_and_the_person_s_yes_is_marked(self):
        """No rule: the form asks; accepted, the row says the person approved."""
        project = self._project()
        result = self._confirmed_call(
            self.key, "delete_project", {"project_id": project.id}
        )
        self.assertFalse(result["isError"], result)
        row = self._row(project)
        self.assertEqual((row.approval, row.approval_rule_id.id), ("person", False))

    def test_declining_a_question_asked_before_the_rule_runs_nothing(self):
        """Asked before the rule, the person's "Decline" stands once it exists:
        the approval is withdrawn, nothing runs and the rule counts no use."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        self.assertEqual(first.get("resultType"), "input_required", first)
        rule = self._grant(self.manager, self.credential)
        for action in ("decline", "cancel"):
            with self.subTest(action=action):
                result = self._call_tool(
                    self.key,
                    "delete_project",
                    args,
                    FORM_CAPABILITIES,
                    requestState=first["requestState"],
                    inputResponses={"confirm": {"action": action}},
                )
                self.assertTrue(result["isError"], result)
                self.assertTrue(project.exists())
        rule.invalidate_recordset()
        self.assertEqual(rule.use_count, 0)
        approval = self.env["incubacloud.mcp.approval"].search(
            [("tool", "=", "delete_project"), ("user_id", "=", self.manager.id)]
        )
        self.assertEqual(approval.mapped("state"), ["cancelled"])

    def test_another_key_of_the_same_person_still_asks(self):
        """A second key, even with the same name, is not the one granted."""
        self._grant(self.manager, self.credential)
        other = self._api_key(self.manager, name="automation")
        project = self._project()
        result = self._call_tool(
            other, "delete_project", {"project_id": project.id}, FORM_CAPABILITIES
        )
        self.assertEqual(result.get("resultType"), "input_required")
        self.assertTrue(project.exists())

    def test_a_job_the_call_queues_carries_the_mark(self):
        """The job a deploy queues says auto, with its rule."""
        rule = self._grant(self.manager, self.credential)
        job_type = self.env["cloud.job.type"].create(
            {"name": "AA deploy", "code": "aa_deploy_probe", "apply_to": "instance"}
        )

        def fake_deploy(instance):
            """Queue a job the way deploy() does, without its preconditions."""
            return (
                instance.env["cloud.job"]
                .sudo()
                .create(
                    {
                        "name": "deploy",
                        "instance_id": instance.id,
                        "host_id": instance.host_id.id,
                        "job_type_id": job_type.id,
                    }
                )
                .id
            )

        self._panel_fixture()
        Catalog = type(self.env["incubacloud.mcp.catalog"])
        real_spec = Catalog._spec

        def asking_spec(catalog, name):
            """deploy_instance as a tool that asks, to queue a job approved."""
            spec = real_spec(catalog, name)
            if name == "deploy_instance":
                return dataclasses.replace(spec, confirm="always")
            return spec

        with (
            patch.object(
                type(self.env["cloud.instance"]),
                "deploy",
                autospec=True,
                side_effect=fake_deploy,
            ),
            patch.object(Catalog, "_spec", autospec=True, side_effect=asking_spec),
        ):
            result = self._call_tool(
                self.key, "deploy_instance", {"instance_id": self.staging.id}
            )
        self.assertFalse(result["isError"], result)
        job = self.env["cloud.job"].browse(self._payload(result)["job_id"])
        self.assertEqual((job.approval, job.approval_rule_id), ("auto", rule))
        self.assertEqual(job._format_history()[0]["approval"], "auto")
        self.assertIn("(test-agent)", job._format()[0]["via_client"])

    def test_a_call_approved_is_audited_even_if_it_writes_nothing(self):
        """Approved by a rule, the call leaves its row though the tool says
        it changes nothing."""
        self._grant(self.manager, self.credential)
        project = self._project()
        Tools = type(self.env["incubacloud.mcp.tools"])
        with patch.object(Tools, "_writes", autospec=True, return_value=False):
            self._call_tool(self.key, "delete_project", {"project_id": project.id})
        self.assertEqual(self._row(project).approval, "auto")

    def test_an_expired_rule_asks_again(self):
        """Past its date the rule approves nothing, without any cron."""
        rule = self._grant(self.manager, self.credential)
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_auto_approval SET expires_at = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(minutes=1), rule.id),
        )
        rule.invalidate_recordset()
        project = self._project()
        result = self._call_tool(
            self.key, "delete_project", {"project_id": project.id}, FORM_CAPABILITIES
        )
        self.assertEqual(result.get("resultType"), "input_required")
        self.assertEqual(rule._state(), "expired")

    def test_revoking_asks_at_the_next_call(self):
        """The person revokes their own rule; the next call asks."""
        rule = self._grant(self.manager, self.credential)
        rule.with_user(self.manager)._revoke_by_person()
        project = self._project()
        result = self._call_tool(
            self.key, "delete_project", {"project_id": project.id}, FORM_CAPABILITIES
        )
        self.assertEqual(result.get("resultType"), "input_required")
        self.assertEqual((rule.revoked_reason, rule.revoked_by_id), ("manual", self.manager))

    def test_what_the_panel_refuses_is_refused_with_a_rule(self):
        """Checks before the question still refuse, and use nothing of the rule."""
        rule = self._grant(self.manager, self.credential)
        self._panel_fixture()
        result = self._call_tool(
            self.key,
            "refresh_staging",
            {
                "instance_id": self.production.id,
                "source_instance_id": self.production.id,
            },
        )
        self.assertTrue(result["isError"])
        rule.invalidate_recordset()
        self.assertEqual(rule.use_count, 0)

    def test_the_listing_and_the_instructions_tell_the_truth(self):
        """With a rule they say the tools run without asking, and until when;
        without one, that the panel asks."""
        listing = self._modern("tools/list", credential=self.key).json()["result"]
        described = {tool["name"]: tool["description"] for tool in listing["tools"]}
        self.assertIn("The panel asks the user to approve it", described["delete_project"])
        rule = self._grant(self.manager, self.credential)
        until = fields.Datetime.to_string(rule.expires_at)
        listing = self._modern("tools/list", credential=self.key).json()["result"]
        described = {tool["name"]: tool["description"] for tool in listing["tools"]}
        self.assertIn(AUTO_LISTED, described["delete_project"])
        self.assertIn(until, described["delete_project"])
        self.assertNotIn(AUTO_LISTED, described["list_projects"])
        discover = self._modern("server/discover", credential=self.key).json()
        instructions = discover["result"]["instructions"]
        self.assertIn("auto-approval rule", instructions)
        self.assertNotIn("wait for the user's approval", instructions)
        other = self._api_key(self.manager, name="other")
        discover = self._modern("server/discover", credential=other).json()
        self.assertIn("wait for the user's approval", discover["result"]["instructions"])


@tagged("post_install", "-at_install")
class TestAutoApprovalRules(AutoApprovalMixin, McpUsersMixin, TransactionCase):
    """Who grants a rule, what it covers, and how it ends."""

    def setUp(self):
        """An Administrator, a developer whose key connected, and a consultant."""
        super().setUp()
        self.registry_enter_test_mode()
        self.admin = self._cloud_user("ar-admin", group="group_cloud_manager")
        self.dev = self._cloud_user("ar-dev", group="group_cloud_developer")
        self.other = self._cloud_user("ar-other", group="group_cloud_developer")
        self.credential = self._key_id(self._api_key(self.dev, name="ci"))
        self._seen(self.dev, self.credential, "API key ci")
        self.Rule = self.env["incubacloud.mcp.auto.approval"]

    def test_a_grant_keeps_who_accepted_which_notice(self):
        """The rule keeps its person, credential, role, date and notice."""
        before = fields.Datetime.now()
        rule = self._grant(self.dev, self.credential, days=30)
        self.assertEqual(
            (
                rule.user_id,
                rule.kind,
                rule.credential,
                rule.credential_label,
                rule.role_at_grant,
                rule.granted_by_id,
                rule.notice_version,
            ),
            (
                self.dev,
                "api_key",
                self.credential,
                "API key ci",
                "developer",
                self.admin,
                NOTICE_VERSION,
            ),
        )
        self.assertGreaterEqual(rule.expires_at, before + timedelta(days=30))
        self.assertEqual(rule._state(), "active")
        row = self.env["cloud.audit.log"].search(
            [("approval_rule_id", "=", rule.id)], limit=1
        )
        self.assertEqual(row.action, "Auto-approval rule granted")

    def test_it_is_not_inherited(self):
        """Another user with the same credential, another credential of the
        same person, and the assistant: none is covered."""
        self._grant(self.dev, self.credential)
        self.assertTrue(self.Rule._active_for(self.dev.id, self.credential))
        self.assertFalse(self.Rule._active_for(self.other.id, self.credential))
        self.assertFalse(self.Rule._active_for(self.dev.id, "API key #-9002"))
        self.assertFalse(
            self.Rule._active_for(self.dev.id, f"client_id {self.credential}")
        )
        self.assertFalse(self.Rule._active_for(self.dev.id, "assistant"))

    def test_only_an_administrator_grants_and_never_through_an_agent(self):
        """Below Administrator, or through an agent's request, it is refused."""
        with self.assertRaises(AccessError):
            self._grant(self.dev, self.credential, admin=self.dev)
        Rule = self.Rule.with_user(self.admin).with_context(
            **{VIA_CONTEXT_KEY: "API key ci (test-agent)"}
        )
        with self.assertRaises(AccessError):
            Rule._grant(
                self.dev.id,
                self.credential,
                days=7,
                acknowledged=True,
                notice_version=NOTICE_VERSION,
            )
        self.assertFalse(self.Rule._live_for(self.dev.id, self.credential))

    def test_the_notice_must_be_accepted_in_its_current_version(self):
        """Without the box, or with another version of the text, refused."""
        with self.assertRaises(UserError):
            self._grant(self.dev, self.credential, acknowledged=False)
        with self.assertRaises(UserError):
            self._grant(self.dev, self.credential, notice_version="2020-01-01")
        with self.assertRaises(UserError):
            self._grant(self.dev, self.credential, acknowledged="yes")

    def test_only_a_credential_of_that_person_that_connected(self):
        """Another person's credential, or one never seen, is refused."""
        with self.assertRaises(UserError):
            self._grant(self.other, self.credential)
        with self.assertRaises(UserError):
            self._grant(self.dev, "API key #424242")

    def test_the_period_is_bounded(self):
        """More than 90 days, none, zero, or a day not after today: refused;
        a date ends that day and never past 90 days."""
        for kwargs in (
            {"days": MAX_DAYS + 1},
            {"days": 0},
            {"days": None},
            {"days": True},
            {"days": None, "expires_on": fields.Date.to_string(fields.Date.today())},
            {
                "days": None,
                "expires_on": fields.Date.to_string(
                    fields.Date.today() + timedelta(days=MAX_DAYS + 2)
                ),
            },
            {"days": None, "expires_on": "not a date"},
        ):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with self.assertRaises(UserError):
                    self._grant(self.dev, self.credential, **kwargs)
        rule = self._grant(
            self.dev,
            self.credential,
            days=None,
            expires_on=fields.Date.to_string(fields.Date.today() + timedelta(days=3)),
        )
        self.assertLessEqual(
            rule.expires_at, fields.Datetime.now() + timedelta(days=MAX_DAYS)
        )
        self.assertGreater(rule.expires_at, fields.Datetime.now() + timedelta(days=2))

    def test_nobody_creates_changes_or_deletes_a_rule_directly(self):
        """create, write and unlink refuse an Administrator, and the date never
        changes, not even with superuser rights."""
        rule = self._grant(self.dev, self.credential)
        as_admin = self.Rule.with_user(self.admin)
        with self.assertRaises(AccessError):
            as_admin.create(
                {
                    "user_id": self.dev.id,
                    "kind": "api_key",
                    "credential": "API key #1",
                    "role_at_grant": "developer",
                    "expires_at": fields.Datetime.now() + timedelta(days=400),
                }
            )
        later = rule.expires_at + timedelta(days=365)
        with self.assertRaises(AccessError):
            rule.with_user(self.admin).write({"expires_at": later})
        with self.assertRaises(AccessError):
            rule.sudo().write({"expires_at": later})
        with self.assertRaises(AccessError):
            rule.with_user(self.admin).unlink()
        with self.assertRaises(AccessError):
            rule.sudo().unlink()

    def test_a_new_rule_replaces_the_live_one(self):
        """Granting again ends the previous rule as superseded."""
        first = self._grant(self.dev, self.credential)
        second = self._grant(self.dev, self.credential, days=1)
        self.assertEqual(first.revoked_reason, "superseded")
        self.assertEqual(self.Rule._active_for(self.dev.id, self.credential), second)

    def test_another_user_cannot_revoke_a_rule_but_an_administrator_can(self):
        """Below Administrator, only one's own rules."""
        rule = self._grant(self.dev, self.credential)
        with self.assertRaises(AccessError):
            rule.with_user(self.other)._revoke_by_person()
        self.assertFalse(rule.revoked_at)
        rule.with_user(self.admin)._revoke_by_person()
        self.assertEqual((rule.revoked_reason, rule.revoked_by_id), ("manual", self.admin))
        self.assertFalse(self.Rule._active_for(self.dev.id, self.credential))

    def test_a_role_that_goes_up_pauses_the_rule(self):
        """Up: paused until another is granted; down: nothing more is allowed."""
        rule = self._grant(self.dev, self.credential)
        manager = self.env.ref("incubacloud.group_cloud_manager")
        self.dev.sudo().write({"group_ids": [(4, manager.id)]})
        self.assertEqual(rule._state(), "paused_role")
        self.assertFalse(self.Rule._active_for(self.dev.id, self.credential))
        self.dev.sudo().write({"group_ids": [(3, manager.id)]})
        self.assertEqual(rule._state(), "active")
        self.assertEqual(rule.role_at_grant, "developer")

    def test_archiving_the_user_revokes_their_rules(self):
        """They do not come back if the user is reactivated."""
        rule = self._grant(self.dev, self.credential)
        self.dev.sudo().active = False
        self.assertEqual(rule.revoked_reason, "user archived")
        self.dev.sudo().active = True
        self.assertFalse(self.Rule._active_for(self.dev.id, self.credential))

    def test_the_platform_switch_pauses_every_rule_and_refuses_new_ones(self):
        """With the hook saying no, existing rules approve nothing."""
        rule = self._grant(self.dev, self.credential)
        self._auto_allowed.return_value = False
        self.assertEqual(rule._state(), "paused_platform")
        self.assertFalse(self.Rule._active_for(self.dev.id, self.credential))
        with self.assertRaises(UserError):
            self._grant(self.dev, self.credential)
        self._auto_allowed.return_value = True
        self.assertEqual(rule._state(), "active")

    def test_use_checks_the_date_against_the_database(self):
        """``_use`` counts while live, and refuses once revoked or expired."""
        rule = self._grant(self.dev, self.credential)
        self.assertTrue(rule._use())
        self.assertEqual(rule.use_count, 1)
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_auto_approval SET expires_at = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(seconds=1), rule.id),
        )
        self.assertFalse(rule._use())

    def test_a_deleted_key_marks_its_rule(self):
        """The vacuum ends the rules of keys that no longer exist."""
        rule = self._grant(self.dev, self.credential)
        self.Rule._gc_deleted_credentials()
        self.assertFalse(rule.revoked_reason)
        self._delete_key(self.credential)
        self.Rule._gc_deleted_credentials()
        self.assertEqual(rule.revoked_reason, "credential deleted")

    def test_the_gate_lets_every_tool_through_with_a_rule(self):
        """For every installed tool that asks: with an active rule, ``_gate``
        runs it and marks it auto, whatever the tool."""
        rule = self._grant(self.dev, self.credential)
        Gate = self.env["incubacloud.mcp.confirm"].with_user(self.dev)
        ctx = CallContext(
            MODERN, True, {}, "test-agent", Principal(self.dev.id, "API key ci", self.credential)
        )
        Tools = type(self.env["incubacloud.mcp.tools"])
        marks = []
        specs = [
            spec
            for spec in self.env["incubacloud.mcp.catalog"]._tool_specs()
            if spec.confirm != "never"
        ]
        self.assertGreater(len(specs), 30)
        with (
            patch.object(
                Tools, "_confirmation", autospec=True, return_value="Approve this."
            ),
            patch.object(mcp_confirm, "set_approval_mark", side_effect=marks.append),
        ):
            for spec in specs:
                with self.subTest(tool=spec.name):
                    self.assertIsNone(Gate._gate(spec, {}, ctx, {}))
        approved = [mark for mark in marks if mark]
        self.assertEqual(len(approved), len(specs))
        self.assertTrue(
            all(mark == {"approval": "auto", "rule": rule.id} for mark in approved)
        )


@tagged("post_install", "-at_install")
class TestAutoApprovalMarks(McpUsersMixin, TransactionCase):
    """The approval's mark cannot be forged on audit rows nor jobs."""

    def test_a_forged_mark_is_dropped_and_cannot_be_written(self):
        """A mark sent by a user is dropped; writing it is refused; the
        context's mark counts only without a request."""
        manager = self._cloud_user("am-mgr", group="group_cloud_manager")
        Log = self.env["cloud.audit.log"].with_user(manager)
        row = Log.create({"action": "Config changed", "approval": "auto"})
        self.assertFalse(row.approval)
        with self.assertRaises(AccessError):
            row.write({"approval": "person"})
        marked = Log.with_context(
            **{APPROVAL_CONTEXT_KEY: {"approval": "person", "rule": 0}}
        ).create({"action": "Config changed"})
        self.assertEqual(marked.approval, "person")
        self.assertEqual(marked._format()[0]["approval"], "person")

    def test_the_switch_is_defined_below_every_layer(self):
        """``_mcp_auto_approval_allowed`` is defined in ``incubacloud`` and in
        no other module of core: a layer that depends on ``incubacloud``
        alone answers after it, whatever the order the MCP loads in."""
        core = {
            "incubacloud",
            "incubacloud_connect",
            "incubacloud_pitr",
            "incubacloud_mcp",
            "incubacloud_mcp_pitr",
            "incubacloud_assistant",
        }
        defining = [
            klass._module
            for klass in type(self.env["cloud.settings"]).__mro__
            if "_mcp_auto_approval_allowed" in vars(klass)
            and getattr(klass, "_module", None) in core
        ]
        self.assertEqual(defining, ["incubacloud"])

    def test_a_rule_s_own_row_keeps_its_rule_under_another_s_mark(self):
        """The row of a grant or a revocation names its rule, also while the
        call that writes it was approved by another rule or by the person;
        a rule named by a user is still dropped."""
        manager = self._cloud_user("am-rules", group="group_cloud_manager")
        Rule = self.env["incubacloud.mcp.auto.approval"].with_context(
            **{_INTERNAL: True}
        )
        approving, revoked = Rule.sudo().create(
            [
                {
                    "user_id": manager.id,
                    "kind": "api_key",
                    "credential": f"API key #-{number}",
                    "role_at_grant": "manager",
                    "expires_at": "2099-01-01 00:00:00",
                }
                for number in (1, 2)
            ]
        )
        for mark in (
            {"approval": "auto", "rule": approving.id},
            {"approval": "person", "rule": 0},
        ):
            with self.subTest(mark=mark["approval"]):
                Log = self.env["cloud.audit.log"].with_context(
                    **{APPROVAL_CONTEXT_KEY: mark}
                )
                own = Log.sudo().create(
                    {
                        "action": "Auto-approval rule revoked",
                        "approval_rule_id": revoked.id,
                    }
                )
                self.assertEqual(
                    (own.approval, own.approval_rule_id), (mark["approval"], revoked)
                )
                forged = Log.with_user(manager).create(
                    {"action": "Config changed", "approval_rule_id": revoked.id}
                )
                self.assertEqual(forged.approval_rule_id.id, mark["rule"] or False)


@tagged("post_install", "-at_install")
class TestCredentialsSeen(AutoApprovalMixin, McpUsersMixin, TransactionCase):
    """The credentials a rule may be granted to."""

    def setUp(self):
        """Two users."""
        super().setUp()
        self.registry_enter_test_mode()
        self.dev = self._cloud_user("cs-dev")
        self.other = self._cloud_user("cs-other")
        self.Seen = self.env["incubacloud.mcp.credential"]

    def _row(self, user, credential):
        """The row of *credential* of *user*."""
        return self.Seen.search(
            [("user_id", "=", user.id), ("client", "=", credential)]
        )

    def test_a_credential_is_stamped_at_most_once_an_hour(self):
        """Seen again within the hour, nothing is written."""
        self._seen(self.dev, "client_id https://c.example/meta.json", "client_id x")
        row = self._row(self.dev, "client_id https://c.example/meta.json")
        self.assertEqual(row.label, "client_id x")
        earlier = fields.Datetime.now() - SEEN_EVERY / 2
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_credential SET last_seen = %s WHERE id = %s",
            (earlier, row.id),
        )
        self._seen(self.dev, "client_id https://c.example/meta.json", "client_id y")
        row.invalidate_recordset()
        self.assertEqual((row.last_seen, row.label), (earlier, "client_id x"))
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_credential SET last_seen = %s WHERE id = %s",
            (earlier - SEEN_EVERY, row.id),
        )
        self._seen(self.dev, "client_id https://c.example/meta.json", "client_id y")
        row.invalidate_recordset()
        self.assertEqual(row.label, "client_id y")
        self.assertGreater(row.last_seen, earlier)

    def test_each_user_gets_only_their_own(self):
        """The candidates of a user are theirs, and never the assistant's key."""
        key = self._key_id(self._api_key(self.dev, name="a"))
        self._seen(self.dev, key, "API key a")
        self._seen(self.other, self._key_id(self._api_key(self.other)), "API key b")
        self._seen(self.dev, "assistant:7", "Assistant (conversation 7)")
        Rule = self.env["incubacloud.mcp.auto.approval"]
        mine = [
            item["credential"]
            for item in Rule._candidates_for(self.dev)
            if item["kind"] != "assistant"
        ]
        self.assertEqual(mine, [key])

    def test_a_deleted_key_is_not_offered(self):
        """Of two keys with the same name, once one is deleted, only the
        other is offered: the deleted one authenticates no more."""
        kept = self._key_id(self._api_key(self.dev, name="ci"))
        gone = self._key_id(self._api_key(self.dev, name="ci"))
        self._seen(self.dev, kept, "API key ci")
        self._seen(self.dev, gone, "API key ci")
        self._delete_key(gone)
        Rule = self.env["incubacloud.mcp.auto.approval"]
        offered = [item["credential"] for item in Rule._candidates_for(self.dev)]
        self.assertIn(kept, offered)
        self.assertNotIn(gone, offered)
        admin = self._cloud_user("cs-admin-gone", group="group_cloud_manager")
        with self.assertRaises(UserError):
            self._grant(self.dev, gone, admin=admin)

    def test_old_credentials_are_forgotten_unless_a_rule_holds_them(self):
        """Not seen for 90 days and without a live rule: removed."""
        admin = self._cloud_user("cs-admin", group="group_cloud_manager")
        old = self._key_id(self._api_key(self.dev, name="old"))
        kept = self._key_id(self._api_key(self.dev, name="kept"))
        self._seen(self.dev, old, "API key old")
        self._seen(self.dev, kept, "API key kept")
        self._grant(self.dev, kept, admin=admin)
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_credential SET last_seen = %s WHERE user_id = %s",
            (fields.Datetime.now() - SEEN_KEPT - timedelta(days=1), self.dev.id),
        )
        self.env.invalidate_all()
        self.Seen._gc_unseen()
        self.assertFalse(self._row(self.dev, old))
        self.assertTrue(self._row(self.dev, kept))


@tagged("post_install", "-at_install")
class TestAutoApprovalRoutes(HttpCase):
    """The routes of the rules are a person's, in the panel."""

    def test_the_routes_are_excluded_from_the_catalog(self):
        """No tool reaches them, each with its reason."""
        policies = self.env["incubacloud.mcp.catalog"]._route_policies()
        for name in ("list", "candidates", "grant", "revoke", "activity"):
            with self.subTest(route=name):
                kind, reason = policies[f"/cloud/mcp/auto_approval/{name}"]
                self.assertEqual(kind, "excluded")
                self.assertIn("never", reason)

    def test_a_grant_through_the_route_needs_an_administrator(self):
        """A developer is refused; the list shows them their own rules only."""
        new_test_user(
            self.env,
            login="aar-dev",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        before = self.env["incubacloud.mcp.auto.approval"].search_count([])
        self.authenticate("aar-dev", "aar-dev")
        body = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "user_id": self.env.uid,
                "credential": "API key #1",
                "days": 7,
                "acknowledged": True,
                "notice_version": NOTICE_VERSION,
            },
        }
        response = self.url_open(
            "/cloud/mcp/auto_approval/grant",
            data=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        refused = response.json()["result"]
        self.assertFalse(refused["ok"])
        self.assertIn("group_cloud_manager", refused["error"])
        self.assertEqual(
            self.env["incubacloud.mcp.auto.approval"].search_count([]), before
        )
        listing = self.url_open(
            "/cloud/mcp/auto_approval/list",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers={"Content-Type": "application/json"},
        ).json()["result"]
        self.assertFalse(listing["can_grant"])
        self.assertEqual(listing["rules"], [])
        self.assertEqual(listing["notice_version"], NOTICE_VERSION)


class TestAutoApprovalCommitted(TransactionCase):
    """A rule's use is committed on its own cursor, before the action runs.

    The transactions are real: the rule is committed on a cursor of its own,
    used from a request's cursor that then rolls back, and removed at the
    end with SQL (a rule cannot be deleted through the ORM).
    """

    def setUp(self):
        """A committed rule of the administrator, live for a day."""
        super().setUp()
        uid = self.env.ref("base.user_admin").id
        with self.env.registry.cursor() as cr:
            cr.execute(
                "INSERT INTO incubacloud_mcp_auto_approval "
                "(user_id, kind, credential, role_at_grant, expires_at, use_count) "
                "VALUES (%s, 'api_key', 'API key #-77', 'manager', "
                "(now() at time zone 'UTC') + interval '1 day', 0) RETURNING id",
                (uid,),
            )
            self.rule_id = cr.fetchone()[0]
            cr.commit()
        self.addCleanup(self._drop)

    def _drop(self):
        """Remove the committed rule."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM incubacloud_mcp_auto_approval WHERE id = %s",
                (self.rule_id,),
            )
            cr.commit()

    def _count(self):
        """The rule's committed count, read on a fresh cursor."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT use_count FROM incubacloud_mcp_auto_approval WHERE id = %s",
                (self.rule_id,),
            )
            return cr.fetchone()[0]

    def test_the_use_is_committed_before_the_action_and_kept_on_rollback(self):
        """Counted at once — no lock held while the action runs, so a parallel
        call does not wait — and still counted when the request rolls back."""
        with self.env.registry.cursor() as request_cr:
            env = self.env(cr=request_cr)
            rule = env["incubacloud.mcp.auto.approval"].sudo().browse(self.rule_id)
            self.assertTrue(rule._use())
            # The request is still open: another call's count is not blocked.
            self.assertEqual(self._count(), 1)
            self.assertTrue(rule._use())
            request_cr.rollback()
        self.assertEqual(self._count(), 2)
