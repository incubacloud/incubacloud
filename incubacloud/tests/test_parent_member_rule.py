"""A row stays within the projects its writer is a member of.

The record rules on instances, their repository, domain and whitelist
rows and the project's repository rows hold a Stakeholder or Consultant
to their member projects. A write is checked against the row as it was,
so the new parent is checked too: a row moved, or an instance created,
under a project of someone else's is refused.
"""
from odoo.exceptions import AccessError

from odoo.addons.incubacloud.tests.test_cloud_security import (
    CloudSecurityBase,
)


class TestParentMemberRule(CloudSecurityBase):

    def setUp(self):
        super().setUp()
        self.consultant = self._create_user(
            "parent_rule_consultant", "group_cloud_consultant",
        )
        self.manager = self._create_user(
            "parent_rule_manager", "group_cloud_manager",
        )
        self.mine = self._create_project("Mine", members=[self.consultant])
        self.mine_too = self._create_project(
            "Mine too", members=[self.consultant],
        )
        self.other = self._create_project("Other")
        self.empty = self._create_project("Empty")
        self.inst_mine = self._create_instance(self.mine)
        self.inst_mine_too = self._create_instance(
            self.mine_too, name="inst-mine-too",
        )
        self.inst_other = self._create_instance(
            self.other, environment="production", name="inst-other",
        )
        Repo = self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True,
        )
        self.repo = Repo.create({
            "instance_id": self.inst_mine.id,
            "url": "https://github.com/example/mine",
            "branch": "main",
        })
        self.domain = self.env["cloud.instance.domain"].create({
            "instance_id": self.inst_mine.id,
            "hostname": "parent-rule-mine.example.com",
        })
        self.whitelist = self.env["cloud.instance.whitelist"].create({
            "instance_id": self.inst_mine.id,
            "hostname": "parent-rule.example.com",
        })
        self.project_repo = self.env["cloud.project.repo"].with_context(
            skip_apply_requirements=True,
        ).create({
            "project_id": self.mine.id,
            "url": "https://github.com/example/mine",
            "branch": "main",
        })

    def _as(self, records, user=None):
        """*records* in *user*'s environment, the consultant's by default."""
        return records.with_user(user or self.consultant).with_context(
            skip_apply_requirements=True,
        )

    def test_child_rows_cannot_move_to_a_foreign_instance(self):
        """Repository, domain and whitelist rows."""
        for row in (self.repo, self.domain, self.whitelist):
            with self.subTest(model=row._name), \
                    self.assertRaises(AccessError):
                self._as(row).write({"instance_id": self.inst_other.id})
            self.assertEqual(row.instance_id, self.inst_mine)

    def test_project_repo_cannot_move_to_a_foreign_project(self):
        """A project's repository row."""
        with self.assertRaises(AccessError):
            self._as(self.project_repo).write({"project_id": self.other.id})
        self.assertEqual(self.project_repo.project_id, self.mine)

    def test_an_instance_cannot_move_to_a_foreign_project(self):
        """With a host and without one."""
        with self.assertRaises(AccessError):
            self._as(self.inst_mine).write({"project_id": self.empty.id})
        self.assertEqual(self.inst_mine.project_id, self.mine)

    def test_an_instance_cannot_be_created_in_a_foreign_project(self):
        """The create runs as superuser for the encrypted fields; the
        caller's rule is applied to what it created all the same."""
        with self.assertRaises(AccessError):
            self.env["cloud.instance"].with_user(
                self.consultant,
            ).with_context(skip_project_repos=True).create({
                "name": "planted",
                "project_id": self.empty.id,
                "environment": "production",
                "odoo_version": "19.0",
                "pip_dependencies": "",
                "apt_dependencies": "",
            })
        self.assertFalse(self.env["cloud.instance"].search([
            ("project_id", "=", self.empty.id),
        ]))

    def test_moves_between_own_projects_still_work(self):
        """A member moves rows between the instances of their projects."""
        for row in (self.repo, self.domain, self.whitelist):
            with self.subTest(model=row._name):
                self._as(row).write({"instance_id": self.inst_mine_too.id})
                self.assertEqual(row.instance_id, self.inst_mine_too)
        self._as(self.project_repo).write({"project_id": self.mine_too.id})
        self.assertEqual(self.project_repo.project_id, self.mine_too)
        own = self.env["cloud.instance"].with_user(self.consultant).create({
            "name": "own-new",
            "project_id": self.mine.id,
            "environment": "production",
        })
        self.assertEqual(own.project_id, self.mine)

    def test_a_manager_moves_rows_anywhere(self):
        """Rules give a manager every project."""
        self._as(self.repo, self.manager).write(
            {"instance_id": self.inst_other.id},
        )
        self.assertEqual(self.repo.instance_id, self.inst_other)
