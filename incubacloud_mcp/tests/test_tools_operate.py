"""Operating tools: what changes, done by the user, marked as the agent's."""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

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

    def test_update_instance_turns_on_the_public_website_info_page(self):
        """The exception to the edge guard is announced and written; the
        instance's page answers with it."""
        from odoo.addons.incubacloud_mcp.models.mcp_tools_operate import (
            _UPDATE_INSTANCE,
        )

        self.assertIn("website_info_public", _UPDATE_INSTANCE["properties"])
        self.assertFalse(self.staging.website_info_public)
        result = self._call_tool(
            self.key,
            "update_instance",
            {"instance_id": self.staging.id, "vals": {"website_info_public": True}},
        )
        self.assertFalse(result["isError"], result)
        self.assertTrue(self.staging.website_info_public)
        payload = self._payload(
            self._call_tool(self.key, "get_instance", {"instance_id": self.staging.id})
        )
        self.assertIs(payload["website_info_public"], True)

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

    def test_a_consultant_s_agent_changes_a_branch_but_not_a_url(self):
        """The route's write refuses another repository (10-nov-2026).

        The same URL with another branch is saved; another URL is
        refused with the role it takes, and the line is left as it was.
        """
        line = (
            self.env["cloud.instance.repo"]
            .with_context(skip_apply_requirements=True)
            .create(
                {
                    "instance_id": self.staging.id,
                    "url": "https://github.com/acme/addons",
                    "branch": "19.0",
                }
            )
        )
        with patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts", autospec=True
        ):
            refused = self._call_tool(
                self.key,
                "update_instance",
                {
                    "instance_id": self.staging.id,
                    "vals": {
                        "repos": [
                            {
                                "id": line.id,
                                "url": "https://github.com/acme/elsewhere",
                                "branch": "19.0",
                            }
                        ]
                    },
                },
            )
            self.assertTrue(refused["isError"], refused)
            self.assertIn("Project Manager", refused["content"][0]["text"])
            self.env.invalidate_all()
            self.assertEqual(line.url, "https://github.com/acme/addons")
            kept = self._call_tool(
                self.key,
                "update_instance",
                {
                    "instance_id": self.staging.id,
                    "vals": {
                        "repos": [
                            {
                                "id": line.id,
                                "url": "https://github.com/acme/addons",
                                "branch": "19.0-dev",
                            }
                        ]
                    },
                },
            )
        self.assertFalse(kept["isError"], kept)
        self.env.invalidate_all()
        self.assertEqual(
            (line.url, line.branch), ("https://github.com/acme/addons", "19.0-dev")
        )

    def test_a_consultant_s_agent_creates_an_instance_with_the_project_s_lines(self):
        """Its lines come from the project's; another repository is refused.

        A Consultant cannot create a repository line, and the copy of the
        project's lines refused the instance (10-nov-2026).
        """
        self.env["cloud.project.repo"].with_context(
            skip_apply_requirements=True
        ).create(
            {
                "project_id": self.project.id,
                "url": "https://github.com/acme/addons",
                "branch": "19.0",
            }
        )
        vals = {
            "project_id": self.project.id,
            "host_id": self.host.id,
            "environment": "staging",
        }
        with patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts", autospec=True
        ):
            created = self._payload(
                self._call_tool(
                    self.key,
                    "create_instance",
                    {"vals": vals | {"name": "agent-copy"}},
                )
            )
            refused = self._call_tool(
                self.key,
                "create_instance",
                {
                    "vals": vals
                    | {
                        "name": "agent-refused",
                        "repos": [{"url": "https://github.com/acme/elsewhere"}],
                    }
                },
            )
        instance = self.env["cloud.instance"].browse(created["id"])
        self.assertEqual(
            instance.repo_ids.read(["url", "branch"]),
            [
                {
                    "id": instance.repo_ids.id,
                    "url": "https://github.com/acme/addons",
                    "branch": "19.0",
                }
            ],
        )
        self.assertTrue(refused["isError"], refused)
        self.assertIn("Project Manager", refused["content"][0]["text"])
        self.assertFalse(
            self.env["cloud.instance"].search([("name", "=", "agent-refused")])
        )

    def test_repointing_production_waits_for_approval(self):
        """A production's branch changed by an agent is not written until
        the user approves it: what production deploys from is code reaching
        production, as a write to a branch it deploys from is."""
        line = (
            self.env["cloud.instance.repo"]
            .with_context(skip_apply_requirements=True)
            .create(
                {
                    "instance_id": self.production.id,
                    "url": "https://github.com/acme/x",
                    "branch": "main",
                }
            )
        )
        with patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts", autospec=True
        ):
            result = self._call_tool(
                self.key,
                "update_instance",
                {
                    "instance_id": self.production.id,
                    "vals": {
                        "repos": [
                            {
                                "id": line.id,
                                "url": "https://github.com/acme/x",
                                "branch": "agent/x",
                            }
                        ]
                    },
                },
            )
        self.assertTrue(result["isError"], result)
        self.assertIn("approval", result["content"][0]["text"])
        self.env.invalidate_all()
        self.assertEqual(line.branch, "main")

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


@tagged("post_install", "-at_install")
class TestProductionCodeAsks(PanelFixtureMixin, McpUsersMixin, TransactionCase):
    """update_instance asks before it changes the repositories or branches a
    production deploys from, as repo_write_files asks before a write to a
    branch a production deploys from."""

    def setUp(self):
        """A consultant member of the project; a repository on each instance."""
        super().setUp()
        self._panel_fixture()
        self.consultant = self._cloud_user(
            "mcp-code-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.consultant.id)]
        Line = self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True
        )
        self.line = Line.create(
            {
                "instance_id": self.production.id,
                "url": "https://github.com/acme/x",
                "branch": "main",
            }
        )
        Line.create(
            {
                "instance_id": self.staging.id,
                "url": "https://github.com/acme/x",
                "branch": "main",
            }
        )

    def _ask(self, instance, vals):
        """Return what the consultant is asked to update *instance* with *vals*."""
        spec = self.env["incubacloud.mcp.catalog"]._spec("update_instance")
        return (
            self.env["incubacloud.mcp.tools"]
            .with_user(self.consultant)
            ._confirmation(spec, {"instance_id": instance.id, "vals": vals})
        )

    def _repo(self, **line):
        """Return a ``repos`` list of one line: production's, changed by *line*."""
        return [
            {"id": self.line.id, "url": "https://github.com/acme/x", "branch": "main"}
            | line
        ]

    def test_another_branch_on_production_asks(self):
        message = self._ask(self.production, {"repos": self._repo(branch="agent/x")})
        self.assertIn('"mcp-acme-prod"', message)
        self.assertIn('"acme/x"', message)
        self.assertIn('"agent/x"', message)

    def test_another_repository_on_production_asks(self):
        message = self._ask(
            self.production,
            {"repos": self._repo(url="https://github.com/stranger/y")},
        )
        self.assertIn('"stranger/y"', message)

    def test_a_repository_added_to_production_asks(self):
        repos = self._repo() + [{"url": "https://github.com/acme/z", "branch": "19.0"}]
        self.assertIn('"acme/z"', self._ask(self.production, {"repos": repos}))

    def test_a_pinned_commit_on_production_asks(self):
        message = self._ask(
            self.production, {"repos": self._repo(commit_sha="a" * 40)}
        )
        self.assertIn("a" * 40, message)

    def test_production_s_own_lines_again_ask_nothing(self):
        """The same repositories in another spelling, their modules changed,
        or one of them removed: production deploys from no new code."""
        self.assertIsNone(
            self._ask(
                self.production,
                {
                    "repos": self._repo(
                        url="git@github.com:ACME/x.git", addons="sale_x"
                    )
                },
            )
        )
        self.assertIsNone(self._ask(self.production, {"repos": []}))

    def test_production_s_other_fields_ask_nothing(self):
        self.assertIsNone(self._ask(self.production, {"auto_update": True}))

    def test_a_staging_s_branch_asks_nothing(self):
        line = self.staging.repo_ids
        self.assertIsNone(
            self._ask(
                self.staging,
                {
                    "repos": [
                        {"id": line.id, "url": line.url, "branch": "agent/x"}
                    ]
                },
            )
        )
