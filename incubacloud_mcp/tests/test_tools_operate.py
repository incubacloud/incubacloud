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

    def test_update_instance_keeps_a_repository_to_its_canonical_url(self):
        """An agent's repository line ends in the model's check (5-nov-2026).

        A URL the build would have cloned with the panel's token from
        somewhere else is refused; another spelling of a github.com
        repository is stored as ``https://github.com/<owner>/<repo>``.
        """
        developer = self._cloud_user("mcp-op-developer", group="group_cloud_developer")
        self.project.member_ids = [(4, developer.id)]
        key = self._api_key(developer)
        lines = self.staging.repo_ids.read(["url", "branch"])
        with patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts", autospec=True
        ):
            refused = self._call_tool(
                key,
                "update_instance",
                {
                    "instance_id": self.staging.id,
                    "vals": {"repos": [{"url": "https://github.com/acme/x?y=1"}]},
                },
            )
            self.assertTrue(refused["isError"], refused)
            self.assertIn(
                "is not a recognised GitHub URL", refused["content"][0]["text"]
            )
            self.env.invalidate_all()
            self.assertEqual(self.staging.repo_ids.read(["url", "branch"]), lines)
            kept = self._call_tool(
                key,
                "update_instance",
                {
                    "instance_id": self.staging.id,
                    "vals": {"repos": [{"url": "git@github.com:acme/x.git"}]},
                },
            )
        self.assertFalse(kept["isError"], kept)
        self.env.invalidate_all()
        self.assertEqual(
            self.staging.repo_ids.mapped("url"), ["https://github.com/acme/x.git"]
        )

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
        # The listing is a read: only the creation is audited.
        rows = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: manage_tags")]
        )
        self.assertEqual(len(rows), 1)
        self.assertIn('"create"', rows.details)

    def test_dismiss_alerts_needs_a_target(self):
        """dismiss_alerts without alert_id or all=true is refused."""
        self.assertTrue(self._call_tool(self.key, "dismiss_alerts", {})["isError"])

    def test_a_project_manager_tool_is_not_a_consultant_s(self):
        """create_project is refused to a consultant, naming the role it needs."""
        result = self._assert_refused_by_role(
            self.key, "create_project", {"vals": {"name": "x"}}
        )
        self.assertIn("group_cloud_project_manager", result["content"][0]["text"])

    def test_reviving_is_not_a_consultant_s(self):
        """revive_instance is refused to a consultant, as the panel's revive is:
        it restores the archived copy, and restoring is a developer's."""
        result = self._assert_refused_by_role(
            self.key, "revive_instance", {"instance_id": self.staging.id}
        )
        self.assertIn("group_cloud_developer", result["content"][0]["text"])


@tagged("post_install", "-at_install")
class TestOperateAnswers(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """What a backup and a project's save answer the agent."""

    def setUp(self):
        """A developer member of the project, with an API key."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.developer = self._cloud_user("mcp-op-answers-dev")
        self.project.member_ids = [(4, self.developer.id)]
        self.key = self._api_key(self.developer)

    def test_a_backup_answers_its_own_job_and_its_chain_s_last(self):
        """backup_job_id takes the copy; job_id lists the backups after it."""
        self.staging._transition("deploying")
        self.staging._transition("deployed")
        payload = self._payload(
            self._call_tool(
                self.key, "create_backup", {"instance_id": self.staging.id}
            )
        )
        Job = self.env["cloud.job"]
        self.assertEqual(
            Job.browse(payload["backup_job_id"]).job_type_id.code, "backup_create"
        )
        self.assertEqual(Job.browse(payload["job_id"]).job_type_id.code, "backup_list")

    def test_a_backup_keeps_the_filestore_unless_told_not_to(self):
        """Without with_filestore the copy takes it, as the panel's route does."""
        self.staging._transition("deploying")
        self.staging._transition("deployed")
        Job = self.env["cloud.job"]
        kept = self._payload(
            self._call_tool(
                self.key, "create_backup", {"instance_id": self.staging.id}
            )
        )
        dropped = self._payload(
            self._call_tool(
                self.key,
                "create_backup",
                {"instance_id": self.staging.id, "with_filestore": False},
            )
        )
        self.assertIs(
            Job.browse(kept["backup_job_id"]).payload["with_filestore"], True
        )
        self.assertIs(
            Job.browse(dropped["backup_job_id"]).payload["with_filestore"], False
        )

    def test_a_save_without_repositories_answers_the_project_s(self):
        """The repositories the project holds, not an empty list."""
        self.env["cloud.project.repo"].create(
            {
                "project_id": self.project.id,
                "url": "https://github.com/acme/addons",
                "branch": "19.0",
            }
        )
        payload = self._payload(
            self._call_tool(
                self.key,
                "update_project",
                {"project_id": self.project.id, "vals": {"description": "x"}},
            )
        )
        self.assertEqual(
            [(repo["url"], repo["branch"]) for repo in payload["repos"]],
            [("https://github.com/acme/addons", "19.0")],
        )
