"""What each role reads of a job: its raw output and the files it made.

A job's log holds the panel's own messages (``system``) and the raw
output of the commands it ran (``stdout``/``stderr``). The files a job
makes (backups, exports) are attachments of the job. Reading logs and
backups is a Developer's (``_check_can_view_logs``,
``_check_can_manage_backups``): a Stakeholder, a Consultant and a
Project Manager read the panel's messages of the jobs they see, and
neither the raw output nor the files.
"""

import base64

from odoo.exceptions import AccessError

from .test_cloud_security import CloudSecurityBase

#: The roles below Developer, by the group each one is given.
LOW_ROLES = {
    "stakeholder": "group_cloud_user",
    "consultant": "group_cloud_consultant",
    "pm": "group_cloud_project_manager",
}
#: The roles that read logs and backups.
TECHNICAL_ROLES = {
    "developer": "group_cloud_developer",
    "manager": "group_cloud_manager",
}


class TestJobOutputByRole(CloudSecurityBase):
    """One user per role, a job of their project with a log and a backup."""

    def setUp(self):
        """Create the users, the job, its log, its file and a project file."""
        super().setUp()
        self.users = {
            role: self._create_user(f"job_output_{role}", group)
            for role, group in (LOW_ROLES | TECHNICAL_ROLES).items()
        }
        # The stakeholder and the consultant are members; from Project
        # Manager up every project is seen.
        self.project = self._create_project(
            "Job output",
            members=[self.users["stakeholder"], self.users["consultant"]],
        )
        host = self.env["cloud.host"].create({
            "name": "job-output-host",
            "ip_address": "10.0.0.84",
            "user": "ubuntu",
            "wildcard_domain": "job-output.example.com",
        })
        instance = self._create_instance(self.project, host_id=host.id)
        self.job = self.env["cloud.job"].sudo().create({
            "name": "Backup",
            "host_id": host.id,
            "instance_id": instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "backup_create")], limit=1,
            ).id,
            "state": "done",
        })
        self.env["cloud.job.log.chunk"].sudo().create([
            {"job_id": self.job.id, "source": source, "content": f"{source} line"}
            for source in ("system", "stdout", "stderr")
        ])
        self.backup = self.env["ir.attachment"].sudo().create({
            "name": "backup.zip",
            "datas": base64.b64encode(b"PK backup").decode(),
            "res_model": "cloud.job",
            "res_id": self.job.id,
        })
        self.project_file = self.env["ir.attachment"].sudo().create({
            "name": "notes.txt",
            "datas": base64.b64encode(b"notes").decode(),
            "res_model": "cloud.project",
            "res_id": self.project.id,
        })

    def _sources(self, role):
        """Return the sources of the job's log chunks *role* reads.

        :param str role: a key of ``self.users``
        :rtype: set
        """
        return set(
            self.env["cloud.job.log.chunk"]
            .with_user(self.users[role])
            .search([("job_id", "=", self.job.id)])
            .mapped("source")
        )

    def _job_files(self, role):
        """Return the attachments of the job *role* finds.

        :param str role: a key of ``self.users``
        :rtype: ir.attachment recordset
        """
        return self.env["ir.attachment"].with_user(self.users[role]).search([
            ("res_model", "=", "cloud.job"),
            ("res_id", "=", self.job.id),
        ])

    def test_every_role_sees_the_job(self):
        """The job itself stays visible to all of them."""
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.assertTrue(
                    self.env["cloud.job"].with_user(user).search(
                        [("id", "=", self.job.id)],
                    ),
                )

    def test_low_roles_read_only_the_panel_s_messages(self):
        """Neither stdout nor stderr below Developer."""
        for role in LOW_ROLES:
            with self.subTest(role=role):
                self.assertEqual(self._sources(role), {"system"})

    def test_technical_roles_read_the_whole_log(self):
        """A Developer and an Administrator read the raw output too."""
        for role in TECHNICAL_ROLES:
            with self.subTest(role=role):
                self.assertEqual(
                    self._sources(role), {"system", "stdout", "stderr"},
                )

    def test_low_roles_do_not_read_a_job_s_files(self):
        """Not found, not read, and the job list offers no download."""
        for role in LOW_ROLES:
            user = self.users[role]
            with self.subTest(role=role):
                self.assertFalse(self._job_files(role))
                with self.assertRaises(AccessError):
                    self.backup.with_user(user).read(["name", "datas"])
                job = self.env["cloud.job"].with_user(user).browse(self.job.id)
                self.assertIsNone(job._format()[0]["download_url"])

    def test_technical_roles_read_a_job_s_files(self):
        """A Developer and an Administrator find, read and download them."""
        for role in TECHNICAL_ROLES:
            user = self.users[role]
            with self.subTest(role=role):
                self.assertEqual(self._job_files(role), self.backup)
                self.assertEqual(
                    self.backup.with_user(user).read(["name"])[0]["name"],
                    "backup.zip",
                )
                job = self.env["cloud.job"].with_user(user).browse(self.job.id)
                self.assertEqual(
                    job._format()[0]["download_url"],
                    f"/web/content/{self.backup.id}?download=true",
                )

    def test_low_roles_still_read_other_files(self):
        """Only a job's files are kept from them: a project's, or one of
        their own that belongs to no record, are not."""
        for role in LOW_ROLES:
            user = self.users[role]
            with self.subTest(role=role):
                self.assertEqual(
                    self.project_file.with_user(user).read(["name"])[0]["name"],
                    "notes.txt",
                )
                own = self.env["ir.attachment"].with_user(user).create({
                    "name": f"{role}.txt",
                    "datas": base64.b64encode(b"own").decode(),
                })
                self.assertEqual(
                    self.env["ir.attachment"].with_user(user).search(
                        [("id", "=", own.id)],
                    ),
                    own,
                )
