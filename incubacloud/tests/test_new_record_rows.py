"""A new instance or host is created with rows of its own.

Their ``create`` runs as the system, so what their x2many fields send is
held to the rows the new record creates; a many2many also links records
the user reads, as the forms do with tags.
"""
from unittest.mock import patch

from odoo.exceptions import AccessError

from odoo.addons.incubacloud.tests.test_cloud_security import (
    CloudSecurityBase,
)

_URL = "https://github.com/example/mine"


class TestANewRecordTakesRowsOfItsOwn(CloudSecurityBase):

    def setUp(self):
        super().setUp()
        self.consultant = self._create_user(
            "own_rows_consultant", "group_cloud_consultant",
        )
        self.developer = self._create_user(
            "own_rows_developer", "group_cloud_developer",
        )
        self.manager = self._create_user(
            "own_rows_manager", "group_cloud_manager",
        )
        self.project = self._create_project(
            "Own rows", members=[self.consultant, self.developer],
        )
        self.elsewhere = self._create_instance(
            self._create_project("Elsewhere"), name="elsewhere",
        )
        self.domain = self.env["cloud.instance.domain"].create({
            "instance_id": self.elsewhere.id, "hostname": "elsewhere.example.com",
        })
        self.whitelist = self.env["cloud.instance.whitelist"].create({
            "instance_id": self.elsewhere.id, "hostname": "api.example.com",
        })
        self.alert = self.env["cloud.alert"].create({
            "code": "own_rows", "message": "As it was", "level": "warning",
            "instance_id": self.elsewhere.id,
        })
        self.line = self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True,
        ).create({"instance_id": self.elsewhere.id, "url": _URL})
        patcher = patch.object(
            type(self.env["cloud.instance"]), "_check_addon_conflicts",
            autospec=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _commands(self):
        """Return what each case sends, by field.

        :rtype: tuple
        """
        return (
            ("domain_ids", [(4, self.domain.id)]),
            ("domain_ids", [(2, self.domain.id)]),
            ("whitelist_ids", [(4, self.whitelist.id)]),
            ("alert_ids", [(1, self.alert.id, {"message": "Changed"})]),
        )

    def _create(self, env, name, **vals):
        """Create a staging of the project in *env*.

        :rtype: cloud.instance
        """
        return env["cloud.instance"].with_context(
            skip_apply_requirements=True,
        ).create({
            "name": name, "project_id": self.project.id,
            "environment": "staging",
        } | vals)

    def _rows_as_they_were(self):
        """Assert the other instance's rows are as the set-up left them."""
        self.assertEqual(
            (self.domain.exists().instance_id, self.whitelist.instance_id,
             self.alert.instance_id, self.alert.message),
            (self.elsewhere, self.elsewhere, self.elsewhere, "As it was"),
        )

    def test_a_consultant_sends_rows_that_exist(self):
        """Refused, whatever the command."""
        env = self.env(user=self.consultant)
        for name, commands in self._commands():
            with self.subTest(field=name, command=commands[0][0]), \
                    self.assertRaises(AccessError), self.env.cr.savepoint():
                self._create(env, "refused", **{name: commands})
            self._rows_as_they_were()
        self.assertFalse(self.env["cloud.instance"].search(
            [("name", "=", "refused")],
        ))

    def test_the_system_sends_rows_that_exist(self):
        """As before."""
        (link_domain, drop_domain, link_row, update_alert) = self._commands()
        inst = self._create(self.env, "system-domain", **dict([link_domain]))
        self.assertEqual(self.domain.instance_id, inst)
        self._create(self.env, "system-drop", **dict([drop_domain]))
        self.assertFalse(self.domain.exists())
        inst = self._create(self.env, "system-row", **dict([link_row]))
        self.assertEqual(self.whitelist.instance_id, inst)
        self._create(self.env, "system-alert", **dict([update_alert]))
        self.assertEqual(self.alert.message, "Changed")

    def test_a_consultant_creates_rows_and_picks_tags(self):
        """As the form does."""
        tag = self.env["cloud.instance.tag"].create({"name": "Own rows"})
        inst = self._create(
            self.env(user=self.consultant), "own",
            domain_ids=[(0, 0, {"hostname": "own.example.com"})],
            whitelist_ids=[(0, 0, {"hostname": "pypi.org"})],
            tag_ids=[(6, 0, [tag.id])],
        )
        self.assertEqual(
            (inst.domain_ids.hostname, inst.whitelist_ids.hostname,
             inst.tag_ids),
            ("own.example.com", "pypi.org", tag),
        )

    def test_a_consultant_cannot_create_an_alert_with_it(self):
        """Nor can they create one at all."""
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._create(
                self.env(user=self.consultant), "with-alert",
                alert_ids=[(0, 0, {
                    "code": "own_rows", "message": "New", "level": "warning",
                })],
            )

    def test_a_developer_sends_a_line_that_exists(self):
        """Refused; a line created takes its provider and path from the URL."""
        env = self.env(user=self.developer)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._create(
                env, "line-refused",
                repo_ids=[(1, self.line.id, {"branch": "19.0"})],
            )
        self.assertEqual(
            (self.line.instance_id, self.line.branch),
            (self.elsewhere, "main"),
        )
        inst = self._create(
            env, "line-created", repo_ids=[(0, 0, {
                "url": _URL, "branch": "19.0",
                "repo_path": "some-group/some-repo",
            })],
        )
        line = inst.repo_ids.filtered(lambda r: r.branch == "19.0")
        self.assertEqual(line.repo_path, "example/mine")

    def test_a_manager_sends_a_host_rows_that_exist(self):
        """A new host is held to rows of its own too."""
        other = self.env["cloud.host"].create({
            "name": "own-rows-host", "ip_address": "192.0.2.92",
            "user": "ubuntu", "wildcard_domain": "own-rows.example.com",
        })
        row = other.whitelist_ids[:1]
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["cloud.host"].with_user(self.manager).create({
                "name": "own-rows-new", "ip_address": "192.0.2.93",
                "user": "ubuntu", "wildcard_domain": "own-rows-new.example.com",
                "whitelist_ids": [(4, row.id)],
            })
        self.assertEqual(row.host_id, other)
