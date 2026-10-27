"""Operating tools: what changes, done by the user, marked as the agent's."""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import CLIENT_ID, JwtMixin, McpHttpMixin, McpUsersMixin, PanelFixtureMixin


@tagged("post_install", "-at_install")
class TestOperateTools(
    PanelFixtureMixin, JwtMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """A consultant's agent operates the project the consultant belongs to."""

    def setUp(self):
        """A consultant member of the project, with a token and an API key."""
        super().setUp()
        resource = self._resource_here()
        self._setup_issuer(resource)
        provider = self._configure_issuer(resource)
        self._panel_fixture()
        self.consultant = self._cloud_user(
            "mcp-op-consultant", group="group_cloud_consultant"
        )
        self.consultant.write({"oauth_provider_id": provider.id, "oauth_uid": "4242"})
        self.project.member_ids = [(4, self.consultant.id)]
        self.key = self._api_key(self.consultant)
        self.job_type = self.env["cloud.job.type"].create(
            {"name": "MCP deploy", "code": "mcp_deploy_probe", "apply_to": "instance"}
        )

    def test_a_deploy_through_an_agent_is_audited_and_its_job_marked(self):
        """The job a deploy queues and its audit row both name the agent."""
        job_type = self.job_type

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

        with patch.object(
            type(self.env["cloud.instance"]),
            "deploy",
            autospec=True,
            side_effect=fake_deploy,
        ):
            payload = self._payload(
                self._call_tool(
                    self._token(), "deploy_instance", {"instance_id": self.staging.id}
                )
            )
        job = self.env["cloud.job"].browse(payload["job_id"])
        via = f"{CLIENT_ID} (test-agent)"
        self.assertEqual(job.via_client, via)
        self.assertIn("state", payload)
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: deploy_instance")], limit=1
        )
        self.assertEqual(
            (row.via_client, row.instance_id, row.user_id),
            (via, self.staging, self.consultant),
        )

    def test_update_instance_refuses_a_password_even_nested(self):
        """A secret among the values is refused, at any depth."""
        for vals, path in (
            ({"postgres_password": "x"}, "arguments.vals.postgres_password"),
            (
                {"odoo_conf": {"postgres_password": "x"}},
                "arguments.vals.odoo_conf.postgres_password",
            ),
        ):
            with self.subTest(vals=vals):
                result = self._call_tool(
                    self.key,
                    "update_instance",
                    {"instance_id": self.staging.id, "vals": vals},
                )
                self.assertTrue(result["isError"])
                self.assertEqual(
                    result["content"][0]["text"],
                    f"Secrets are changed in the panel ({path}).",
                )

    def test_update_instance_writes_what_the_route_allows(self):
        """A field save_instance accepts is written."""
        self.staging.auto_update = False
        result = self._call_tool(
            self.key,
            "update_instance",
            {"instance_id": self.staging.id, "vals": {"auto_update": True}},
        )
        self.assertFalse(result["isError"], result)
        self.assertTrue(self.staging.auto_update)

    def test_tags_are_listed_and_created(self):
        """A tag is created, then listed."""
        created = self._payload(
            self._call_tool(
                self.key,
                "manage_tags",
                {"action": "create", "name": "mcp-tag", "scope": "project"},
            )
        )
        self.assertTrue(created)
        listed = self._payload(
            self._call_tool(self.key, "manage_tags", {"action": "list"})
        )
        self.assertIn("mcp-tag", [tag["name"] for tag in listed["tags"]])

    def test_dismiss_alerts_needs_a_target(self):
        """dismiss_alerts without alert_id or all=true is refused."""
        self.assertTrue(self._call_tool(self.key, "dismiss_alerts", {})["isError"])

    def test_a_project_manager_tool_is_not_a_consultant_s(self):
        """create_project is refused to a consultant, naming the role it needs."""
        result = self._call_tool(self.key, "create_project", {"vals": {"name": "x"}})
        self.assertTrue(result["isError"])
        self.assertIn("group_cloud_project_manager", result["content"][0]["text"])
