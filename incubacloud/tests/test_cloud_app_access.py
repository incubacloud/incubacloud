"""Who sees the Cloud app: an internal user with an IncubaCloud role.

Every role implies Stakeholder (``group_cloud_user``). An internal user
with none of them saw the Cloud app in the menu and could open
``/cloud``, where every call then failed on the models' access rules
("Could not load the projects"). The menu is now for Stakeholders up,
and the panel's pages answer such a user as a page that does not exist.
"""

from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase


@tagged("post_install", "-at_install")
class TestCloudAppAccess(HttpCase):
    """The Cloud menu and the panel's pages, by whether the user has a role."""

    def setUp(self):
        """Create a user with no role, a Stakeholder, an Administrator and a job.

        Users of their own, with passwords the test knows: a copy of a
        real panel does not give the admin the password ``admin``.
        """
        super().setUp()
        self.no_role = new_test_user(
            self.env, login="app-no-role", password="app-no-role-pw",
            groups="base.group_user",
        )
        self.stakeholder = new_test_user(
            self.env, login="app-stakeholder", password="app-stakeholder-pw",
            groups="base.group_user,incubacloud.group_cloud_user",
        )
        new_test_user(
            self.env, login="app-manager", password="app-manager-pw",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        host = self.env["cloud.host"].create({
            "name": "app-access-host",
            "ip_address": "10.0.0.83",
            "user": "ubuntu",
            "wildcard_domain": "app-access.example.com",
        })
        project = self.env["cloud.project"].create({"name": "App access"})
        instance = self.env["cloud.instance"].create({
            "name": "app-access-inst",
            "project_id": project.id,
            "host_id": host.id,
            "environment": "staging",
            "state": "deployed",
        })
        self.job = self.env["cloud.job"].create({
            "host_id": host.id,
            "instance_id": instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "rebuild_instance")],
            ).id,
            "state": "done",
        })
        self.menu = self.env.ref("incubacloud.incubacloud_menu_root")

    def _status(self, path):
        """GET *path* on the current session, without following a redirect.

        :param str path: the address on this database
        :return: the HTTP status
        :rtype: int
        """
        return self.url_open(path, allow_redirects=False).status_code

    def _sees_the_menu(self, user):
        """Return whether *user*'s menus include the Cloud app.

        :param user: a ``res.users`` record
        :rtype: bool
        """
        visible = self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()
        return self.menu.id in visible

    def test_a_user_with_no_role_does_not_see_the_app(self):
        """No Cloud in the menu, and the panel's address is not found."""
        self.assertFalse(self._sees_the_menu(self.no_role))
        self.authenticate("app-no-role", "app-no-role-pw")
        for path in ("/cloud", "/cloud/projects"):
            with self.subTest(path=path):
                self.assertEqual(self._status(path), 404)

    def test_a_stakeholder_sees_the_app(self):
        """The lowest role has the menu and opens the panel."""
        self.assertTrue(self._sees_the_menu(self.stakeholder))
        self.authenticate("app-stakeholder", "app-stakeholder-pw")
        for path in ("/cloud", "/cloud/projects"):
            with self.subTest(path=path):
                self.assertEqual(self._status(path), 200)

    def test_a_user_with_no_role_does_not_find_a_jobs_log_page(self):
        """``/cloud/log/<id>`` of a job that exists answers 404, not 403.

        A 403 told that the job exists; the page is not theirs either way.
        """
        self.authenticate("app-no-role", "app-no-role-pw")
        self.assertEqual(self._status(f"/cloud/log/{self.job.id}"), 404)
        self.authenticate("app-manager", "app-manager-pw")
        self.assertEqual(self._status(f"/cloud/log/{self.job.id}"), 200)

    def test_a_user_with_no_role_does_not_download_a_jobs_log(self):
        """``/cloud/log/<id>/download`` answers 404 to them as well."""
        path = f"/cloud/log/{self.job.id}/download"
        self.authenticate("app-no-role", "app-no-role-pw")
        self.assertEqual(self._status(path), 404)
        self.authenticate("app-manager", "app-manager-pw")
        self.assertEqual(self._status(path), 200)
