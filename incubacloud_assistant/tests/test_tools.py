"""What the model may call, what asks first, and the card instead of a link."""

import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.protocol.context import CallContext, Principal
from odoo.addons.incubacloud_mcp.protocol.versions import MODERN

from ..models import assistant_tools
from ..request_scope import call_context
from .common import AssistantMixin


@tagged("post_install", "-at_install")
class TestAssistantTools(AssistantMixin, TransactionCase):
    """The tools of each role, the assistant's rules on top of the MCP's."""

    def setUp(self):
        """A consultant, a developer and a manager; the panel's fixture."""
        super().setUp()
        self._panel_fixture()
        self.consultant = self._cloud_user(
            "asis-tools-c", group="group_cloud_consultant"
        )
        self.developer = self._cloud_user("asis-tools-d")
        self.manager = self._cloud_user("asis-tools-m", group="group_cloud_manager")
        self.project.member_ids = [(4, self.consultant.id), (4, self.developer.id)]

    def _names(self, user):
        """The names of the tools *user*'s model sees."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(user)
        return [definition.name for definition in Tools._definitions()]

    def test_each_role_sees_its_tools_and_staging_needs_a_developer(self):
        """A consultant reads and searches; a developer deploys and writes code."""
        consultant = self._names(self.consultant)
        self.assertIn("get_project", consultant)
        self.assertIn("search_docs", consultant)
        for name in (
            "deploy_instance",
            "create_instance",
            "repo_write_files",
            "get_host",
        ):
            self.assertNotIn(name, consultant)
        developer = self._names(self.developer)
        for name in ("deploy_instance", "create_instance", "repo_write_files"):
            self.assertIn(name, developer)
        self.assertIn("get_host", self._names(self.manager))
        self.assertEqual(developer, sorted(developer))

    def test_creating_an_instance_and_changing_repositories_ask_first(self):
        """Resources of the server; the repositories added, removed and moved."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.developer)
        Catalog = self.env["incubacloud.mcp.catalog"]
        create = Catalog._spec("create_instance")
        self.assertIn(
            "CPU",
            Tools._extra_confirmation(
                create, {"vals": {"name": "acme-try", "environment": "staging"}}
            ),
        )
        self.project.sudo().with_context(skip_apply_requirements=True).repo_ids = [
            (0, 0, {"url": "https://github.com/acme/old", "branch": "19.0"})
        ]
        update = Catalog._spec("update_project")
        card = Tools._extra_confirmation(
            update,
            {
                "project_id": self.project.id,
                "vals": {"repos": [{"url": "git@github.com:acme/new.git"}]},
            },
        )
        self.assertIn('"acme/new"', card)
        self.assertIn('removes "acme/old"', card)
        renamed = Tools._extra_confirmation(
            update, {"project_id": self.project.id, "vals": {"name": "x"}}
        )
        self.assertIn("update_project", renamed)

    def test_production_asks_first_staging_does_not_reading_never(self):
        """A deploy of production asks; of the staging, not; a read never."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.developer)
        Catalog = self.env["incubacloud.mcp.catalog"]
        deploy = Catalog._spec("deploy_instance")
        self.assertIn(
            self.production.name,
            Tools._extra_confirmation(deploy, {"instance_id": self.production.id}),
        )
        self.assertTrue(
            Tools._acts_on_production(deploy, {"instance_id": self.production.id})
        )
        self.assertIsNone(
            Tools._extra_confirmation(deploy, {"instance_id": self.staging.id})
        )
        read = Catalog._spec("get_instance")
        self.assertIsNone(
            Tools._extra_confirmation(read, {"instance_id": self.production.id})
        )

    def test_every_change_asks_unless_it_is_safe_on_a_staging(self):
        """Each tool that is not read-only asks, or is one of the few that do not."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.manager)
        specs = self.env["incubacloud.mcp.catalog"]._tool_specs()
        self.assertLessEqual(assistant_tools._UNASKED, {spec.name for spec in specs})
        for spec in specs:
            if spec.read_only or spec.name in assistant_tools._UNASKED:
                continue
            with self.subTest(spec.name):
                self.assertTrue(Tools._extra_confirmation(spec, {}))

    def test_production_is_found_by_any_id_of_the_arguments(self):
        """A repository line of production, the server production runs on."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.manager)
        Catalog = self.env["incubacloud.mcp.catalog"]
        line = (
            self.env["cloud.instance.repo"]
            .sudo()
            .with_context(skip_apply_requirements=True)
            .create(
                {
                    "instance_id": self.production.id,
                    "url": "https://github.com/acme/odoo-addons",
                    "branch": "19.0",
                }
            )
        )
        for name, args in (
            ("freeze_repos", {"model": "cloud.instance.repo", "repo_ids": [line.id]}),
            ("harden_host", {"host_id": self.host.id}),
        ):
            with self.subTest(name):
                card = Tools._extra_confirmation(Catalog._spec(name), args)
                self.assertIn(self.production.name, card)

    def test_run_command_and_an_instance_s_repositories_always_ask(self):
        """On a staging too: a command, and the repositories it deploys from."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.developer)
        Catalog = self.env["incubacloud.mcp.catalog"]
        command = Tools._extra_confirmation(
            Catalog._spec("run_command"),
            {"instance_id": self.staging.id, "command": "odoo shell"},
        )
        self.assertIn("odoo shell", command)
        repos = Tools._extra_confirmation(
            Catalog._spec("update_instance"),
            {
                "instance_id": self.staging.id,
                "vals": {
                    "repos": [{"url": "https://github.com/acme/x", "branch": "b"}]
                },
            },
        )
        self.assertIn('"acme/x"', repos)

    def test_after_content_others_can_write_every_change_asks(self):
        """A staging's deploy asks once the task read a log; a read never."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.developer)
        Catalog = self.env["incubacloud.mcp.catalog"]
        deploy = Catalog._spec("deploy_instance")
        args = {"instance_id": self.staging.id}
        self.assertIsNone(Tools._extra_confirmation(deploy, args))
        self.assertIn(
            "read content", Tools._extra_confirmation(deploy, args, tainted=True)
        )
        read = Catalog._spec("read_logs")
        self.assertIsNone(Tools._extra_confirmation(read, args, tainted=True))

    def test_the_owner_s_switch_decides_about_the_agent_s_own_work(self):
        """Tainted, a write to the agent's own branch asks; with the switch off, not."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.developer)
        Branch = self.env["incubacloud.mcp.agent.branch"].with_user(self.developer)
        Branch._remember("acme/odoo-addons", "assistant/x")
        spec = self.env["incubacloud.mcp.catalog"]._spec("repo_write_files")
        args = {
            "url": "https://github.com/acme/odoo-addons",
            "branch": "assistant/x",
            "message": "Add acme_code",
            "files": [{"path": "acme_code/__init__.py", "content": ""}],
            "public": False,
        }
        self.assertIsNone(Tools._extra_confirmation(spec, args))
        tainted = Tools._extra_confirmation(spec, args, tainted=True)
        self.assertIn("read content", tainted)
        with patch.object(assistant_tools, "TAINT_COVERS_OWN_WORK", False):
            self.assertIsNone(Tools._extra_confirmation(spec, args, tainted=True))

    def test_a_production_the_user_does_not_see_is_not_named(self):
        """Found through its server, a production out of reach stays nameless."""
        outsider = self._cloud_user("asis-tools-out", group="group_cloud_consultant")
        Tools = self.env["incubacloud.assistant.tools"].with_user(outsider)
        spec = self.env["incubacloud.mcp.catalog"]._spec("harden_host")
        card = Tools._extra_confirmation(spec, {"host_id": self.host.id})
        self.assertIn("a production instance", card)
        self.assertNotIn(self.production.name, card)

    def test_the_assistant_gets_a_card_bound_to_its_conversation(self):
        """A card, not a link; approved, used once by that conversation only.

        In registry test mode: an approval is used on a cursor of its own.
        """
        Confirm = self.env["incubacloud.mcp.confirm"].with_user(self.developer)
        spec = self.env["incubacloud.mcp.catalog"]._spec("deploy_instance")
        args = {"instance_id": self.production.id}
        ctx = call_context(self.developer.id, 41)
        with self.enter_registry_test_mode():
            stop = Confirm._gate(spec, args, ctx, {})
            approval = self.env["incubacloud.mcp.approval"].browse(
                stop["assistant_confirmation"]
            )
            self.assertEqual(
                (approval.channel, approval.credential, approval.state),
                ("assistant", "assistant:41", "pending"),
            )
            self.assertTrue(approval._decide(True))
            other = Confirm._gate(spec, args, call_context(self.developer.id, 42), {})
            self.assertIn("assistant_confirmation", other)
            self.assertIsNone(Confirm._gate(spec, args, ctx, {}))
            approval.invalidate_recordset()
            self.assertEqual(approval.state, "used")
            self.assertIn("assistant_confirmation", Confirm._gate(spec, args, ctx, {}))

    def test_an_agent_outside_keeps_the_mcp_s_rules(self):
        """The assistant's own rules (production) never reach an external agent."""
        Confirm = self.env["incubacloud.mcp.confirm"].with_user(self.developer)
        spec = self.env["incubacloud.mcp.catalog"]._spec("deploy_instance")
        ctx = CallContext(
            MODERN, True, {}, "agent", Principal(self.developer.id, "client", "client")
        )
        self.assertIsNone(
            Confirm._gate(spec, {"instance_id": self.production.id}, ctx, {})
        )

    def test_the_assistant_s_own_tools_answer_and_refuse(self):
        """The documentation answers; a path out of the code is an error."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.consultant)
        text, is_error = Tools._run_local(
            Tools._local_spec("search_docs"), {"query": "backup"}
        )
        self.assertFalse(is_error, text)
        self.assertIn("docs.incubacloud.io", text)
        text, is_error = Tools._run_local(
            Tools._local_spec("read_code"), {"path": "../x"}
        )
        self.assertTrue(is_error)
        text, is_error = Tools._run_local(
            Tools._local_spec("search_code"), {"txt": "x"}
        )
        self.assertTrue(is_error)
        self.assertIn("unknown argument", text)
        self.assertTrue(
            json.loads(
                Tools._run_local(
                    Tools._local_spec("read_code"), {"path": "incubacloud"}
                )[0]
            )["entries"]
        )
