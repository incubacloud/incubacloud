"""A new instance or host is created with rows of its own.

Their ``create`` runs as the system, so what their x2many fields send is
held to the rows the new record creates; a many2many also links records
the user reads, as the forms do with tags. The defaults of the context
and of ``ir.default`` are held to the same rules as what is sent.
"""
from unittest.mock import patch

from odoo.exceptions import AccessError

from odoo.addons.incubacloud.tests.test_cloud_security import (
    CloudSecurityBase,
)

_URL = "https://github.com/example/mine"
_FOREIGN_URL = "https://github.com/example/foreign"
_OWN_ROWS = "can only create"
#: A default that links or changes a row the user may not write: Odoo's
#: ``default_get`` refuses it before the panel's check reads it, once its
#: 19.0 checks the commands of the context (odoo/odoo#285233); an older
#: 19.0 leaves the refusal to the panel. Either keeps the row where it was.
_OWN_ROWS_OR_ODOOS = _OWN_ROWS + "|doesn't have 'write' access"


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

    def _create(self, env, name, context=None, **vals):
        """Create a staging of the project in *env*, with *context* added.

        :rtype: cloud.instance
        """
        return env["cloud.instance"].with_context(
            skip_apply_requirements=True, **(context or {}),
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
        """Refused, whatever the command, and a list of ids too."""
        env = self.env(user=self.consultant)
        for name, commands in self._commands() + (
            ("domain_ids", [(6, 0, [self.domain.id])]),
            ("domain_ids", [self.domain.id]),
            ("domain_ids", [(3, self.domain.id)]),
        ):
            with self.subTest(field=name, value=commands), \
                    self.assertRaisesRegex(AccessError, _OWN_ROWS), \
                    self.env.cr.savepoint():
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

    def test_a_tuple_of_commands_is_read_as_its_commands(self):
        """As a list of them: rows created and tags picked, rows that
        exist refused."""
        env = self.env(user=self.consultant)
        tag = self.env["cloud.instance.tag"].create({"name": "Tuple"})
        inst = self._create(
            env, "tuple",
            domain_ids=((0, 0, {"hostname": "tuple.example.com"}),),
            tag_ids=((4, tag.id),),
        )
        self.assertEqual(
            (inst.domain_ids.hostname, inst.tag_ids),
            ("tuple.example.com", tag),
        )
        with self.assertRaisesRegex(AccessError, _OWN_ROWS), \
                self.env.cr.savepoint():
            self._create(env, "refused", domain_ids=((4, self.domain.id),))
        self._rows_as_they_were()

    def test_a_consultant_cannot_create_an_alert_with_it(self):
        """Nor can they create one at all."""
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._create(
                self.env(user=self.consultant), "with-alert",
                alert_ids=[(0, 0, {
                    "code": "own_rows", "message": "New", "level": "warning",
                })],
            )

    def test_a_developer_changes_a_line_that_exists(self):
        """Refused: the other instance's line keeps its instance and branch."""
        env = self.env(user=self.developer)
        with self.assertRaisesRegex(AccessError, _OWN_ROWS), \
                self.env.cr.savepoint():
            self._create(
                env, "line-refused",
                repo_ids=[(1, self.line.id, {"branch": "19.0"})],
            )
        self.assertEqual(
            (self.line.instance_id, self.line.branch),
            (self.elsewhere, "main"),
        )

    def test_a_line_created_with_an_instance_follows_its_url(self):
        """Its path is the URL's, not the one sent."""
        inst = self._create(
            self.env(user=self.developer), "line-created",
            repo_ids=[(0, 0, {
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
        Host = self.env["cloud.host"].with_user(self.manager)
        new_host = {
            "name": "own-rows-new", "ip_address": "192.0.2.93",
            "user": "ubuntu", "wildcard_domain": "own-rows-new.example.com",
        }
        for chosen, vals in (
            (Host, {"whitelist_ids": [(4, row.id)]}),
            (Host.with_context(default_whitelist_ids=[(4, row.id)]), {}),
        ):
            with self.subTest(context=chosen.env.context), \
                    self.assertRaisesRegex(AccessError, _OWN_ROWS), \
                    self.env.cr.savepoint():
                chosen.create(new_host | vals)
        self.assertEqual(row.host_id, other)

    def test_a_row_created_with_it_takes_rows_of_its_own(self):
        """A new host's new instance does not take another's domain."""
        with self.assertRaisesRegex(AccessError, _OWN_ROWS), \
                self.env.cr.savepoint():
            self.env["cloud.host"].with_user(self.manager).create({
                "name": "own-rows-nested", "ip_address": "192.0.2.94",
                "user": "ubuntu",
                "wildcard_domain": "own-rows-nested.example.com",
                "instance_ids": [(0, 0, {
                    "name": "nested", "project_id": self.project.id,
                    "environment": "staging",
                    "domain_ids": [(4, self.domain.id)],
                })],
            })
        self._rows_as_they_were()

    def test_a_consultant_chooses_rows_that_exist_as_defaults(self):
        """Refused, and so is an alert or a repository of another project."""
        env = self.env(user=self.consultant)
        for context, error in (
            ({"default_domain_ids": [(4, self.domain.id)]}, _OWN_ROWS_OR_ODOOS),
            ({"default_whitelist_ids": [(4, self.whitelist.id)]},
             _OWN_ROWS_OR_ODOOS),
            ({"default_alert_ids": [(1, self.alert.id, {"message": "Changed"})]},
             _OWN_ROWS_OR_ODOOS),
            ({"default_alert_ids": [(0, 0, {
                "code": "own_rows", "message": "New", "level": "warning",
            })]}, "not allowed"),
            ({"default_repo_ids": [(0, 0, {"url": _FOREIGN_URL})]},
             "Only a Project Manager"),
            ({"default_state": "deployed"}, "past draft"),
        ):
            with self.subTest(context=context), \
                    self.assertRaisesRegex(AccessError, error), \
                    self.env.cr.savepoint():
                self._create(env, "refused", context=context)
            self._rows_as_they_were()
        self.assertFalse(self.env["cloud.instance"].search(
            [("name", "=", "refused")],
        ))
        self.assertFalse(self.env["cloud.instance.repo"].search(
            [("url", "=", _FOREIGN_URL)],
        ))

    def test_a_consultant_keeps_rows_that_exist_as_their_own_default(self):
        """A default of their own in ``ir.default`` is refused, and one saved
        before is not read."""
        with self.assertRaisesRegex(AccessError, "Only an Administrator"), \
                self.env.cr.savepoint():
            self.env["ir.default"].with_user(self.consultant).set(
                "cloud.instance", "domain_ids", [(4, self.domain.id)],
                user_id=True,
            )
        self.env["ir.default"].set(
            "cloud.instance", "domain_ids", [(4, self.domain.id)],
            user_id=self.consultant.id,
        )
        inst = self._create(self.env(user=self.consultant), "not-read")
        self.assertFalse(inst.sudo().domain_ids)
        self._rows_as_they_were()

    def test_a_row_it_created_is_checked_with_the_users_rules(self):
        """A row of a model the user may create, refused by a rule of its
        model once created: the instance is not created either."""
        self.env["ir.rule"].create({
            "name": "No refused-row domains",
            "model_id": self.env["ir.model"]._get_id("cloud.instance.domain"),
            "domain_force": "[('hostname', '!=', 'refused-row.example.com')]",
            "perm_read": False, "perm_write": False, "perm_unlink": False,
            "perm_create": True,
        })
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self._create(
                self.env(user=self.consultant), "refused-row",
                domain_ids=[(0, 0, {"hostname": "refused-row.example.com"})],
            )
        self.assertFalse(self.env["cloud.instance"].search(
            [("name", "=", "refused-row")],
        ))

    def test_the_system_chooses_them(self):
        """As before."""
        inst = self._create(self.env, "system-defaults", context={
            "default_domain_ids": [(4, self.domain.id)],
            "default_repo_ids": [(0, 0, {"url": _FOREIGN_URL})],
            "default_state": "deployed",
        })
        self.assertEqual(
            (self.domain.instance_id, inst.state),
            (inst, "deployed"),
        )
        self.assertIn(_FOREIGN_URL, inst.repo_ids.mapped("url"))

    def test_a_consultant_creates_an_instance_with_a_default_project(self):
        """As an action opened on a project does."""
        inst = self.env["cloud.instance"].with_user(self.consultant).with_context(
            skip_apply_requirements=True, default_project_id=self.project.id,
        ).create({"name": "by-default", "environment": "staging"})
        self.assertEqual(inst.project_id, self.project)
