"""The assistant's get_first_steps: the panel's first steps, in English, no names.

It reads ``cloud.first.steps._state_for_assistant``, the source of the home
page's card: the same steps, in English whatever the person's language,
without the name or id of a server, a project or an instance.
"""

import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.tests.common import McpUsersMixin

from ..models.assistant_prompt import system_prompt


@tagged("post_install", "-at_install")
class TestFirstStepsTool(McpUsersMixin, TransactionCase):
    """get_first_steps for every role, in English, absent when the steps are off."""

    def setUp(self):
        """A user and a manager of the panel, both writing in Spanish."""
        super().setUp()
        self.env["res.lang"]._activate_lang("es_ES")
        self.user = self._cloud_user("asis-fs-u", group="group_cloud_user")
        self.manager = self._cloud_user("asis-fs-m", group="group_cloud_manager")
        (self.user | self.manager).lang = "es_ES"
        self.env.cr.execute("DELETE FROM cloud_first_step")
        # A layer may turn the first steps off (the master's does): these
        # are tests of core's tool, so they stay on, on the registry's class.
        self.StepsClass = type(self.env["cloud.first.steps"])
        patcher = patch.object(self.StepsClass, "_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # On a copy of a running panel only the servers a test creates
        # count, so the server step is the next one as on a new panel.
        before = self.env["cloud.host"].sudo().with_context(active_test=False).search([]).ids
        layer = self.StepsClass._first_step_host_domain

        def domain(model):
            """The layer's servers, without those the database held before."""
            return [*layer(model), ("id", "not in", before)]

        patcher = patch.object(self.StepsClass, "_first_step_host_domain", domain)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tools(self, user):
        """The assistant's tools as *user*."""
        return self.env["incubacloud.assistant.tools"].with_user(user)

    def _answer(self, user):
        """What get_first_steps answers *user*, decoded."""
        tools = self._tools(user)
        text, is_error = tools._run_local(tools._local_spec("get_first_steps"), {})
        self.assertFalse(is_error, text)
        return json.loads(text)

    def _names(self, user):
        """The tools *user*'s model sees, by name."""
        return [spec.name for spec in self._tools(user)._visible_specs()]

    def test_every_role_has_it_read_only(self):
        self.assertIn("get_first_steps", self._names(self.user))
        spec = self._tools(self.user)._local_spec("get_first_steps")
        self.assertTrue(spec.read_only)

    def test_it_answers_in_english_with_the_next_step(self):
        answer = self._answer(self.manager)
        self.assertTrue(answer["can_do_them"])
        self.assertEqual(answer["next"], "server")
        server = answer["steps"][0]
        self.assertEqual(server["title"], "Get a server")
        # Core's button first; a layer may add its own after it.
        self.assertEqual(server["actions"][0], {"button": "Connect a server", "screen": "New Host"})
        english = self.env["cloud.first.steps"].with_context(lang="en_US")._steps()[0]
        self.assertEqual(server["actions"], [
            {"button": action["label"], "screen": action["screen"]}
            for action in english["actions"]
        ])
        self.assertTrue(server["guide_url"].startswith("https://docs.incubacloud.io/"))
        # The card shows the same step in the person's language.
        card = self.env["cloud.first.steps"].with_user(self.manager).with_context(
            lang="es_ES",
        )._state(self.manager)
        self.assertEqual(card["steps"][0]["title"], "Consiga un servidor")
        self.assertFalse(self._answer(self.user)["can_do_them"])

    def test_it_names_no_record(self):
        host = self.env["cloud.host"].create({
            "name": "asis-fs-secret-host", "ip_address": "10.0.13.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "asis-fs.example.com",
        })
        backend = self.env["cloud.backup.backend"].create({
            "name": "asis-fs-secret-backend", "backend_type": "s3",
            "s3_bucket": "asis-fs-bucket", "s3_path": "backups",
            "s3_access_key_id": "AKIAASIS", "s3_secret_access_key": "shh",
        })
        project = self.env["cloud.project"].create({"name": "asis-fs-secret-project"})
        self.env["cloud.instance"].create({
            "name": "asis-fs-secret-instance", "project_id": project.id,
            "environment": "production", "host_id": host.id,
            "backup_backend_id": backend.id,
        })
        text = json.dumps(self._answer(self.manager))
        for secret in ("asis-fs-secret", "project_id", "instance_id", "host_id"):
            self.assertNotIn(secret, text)
        # The deploy step leads to the oldest project's production: this
        # test's on a new panel, a project of its own on a running one.
        Steps = self.env["cloud.first.steps"].with_user(self.manager).with_context(
            lang="en_US",
        )
        label = Steps._deploy_actions()[0]["label"]
        self.assertIn(f'"button": "{label}"', text)
        if self.env["cloud.project"].search([], order="id", limit=1) == project:
            self.assertEqual(label, "Open the instance")

    def test_absent_when_the_panel_keeps_no_first_steps(self):
        with patch.object(self.StepsClass, "_enabled", return_value=False):
            self.assertNotIn("get_first_steps", self._names(self.manager))
            tools = self._tools(self.manager)
            self.assertFalse(tools._allowed(tools._local_spec("get_first_steps")))

    def test_the_prompt_says_when_to_call_it(self):
        prompt = system_prompt("English")
        self.assertIn("call get_first_steps and answer with its next step", prompt)
