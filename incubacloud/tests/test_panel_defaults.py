"""What a user's defaults and context may decide on the panel's records.

A personal default (``ir.default`` with a user) on the panel's models is
an Administrator's: refused to anyone else, not read for them, and those
saved before 1.0.223 are deleted. The audit rows name who acted and when,
whatever the context sends; the lifecycle state, the endpoint guard and
the platform's alerts are not reached through the context or ``call_kw``.
"""
import importlib.util
import os

from odoo import api, fields
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.service.model import get_public_method
from odoo.tests import new_test_user, tagged

from odoo.addons.incubacloud.tests.test_cloud_host import KnownHostsCase
from odoo.addons.incubacloud.tests.test_cloud_security import CloudSecurityBase
from odoo.addons.incubacloud.tests.test_job_queue import _QueueCase

_ADMIN_ONLY = "Only an Administrator sets personal default"


def _load_migration():
    """Import the 1.0.223 post-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.223",
        "post-migrate.py",
    )
    spec = importlib.util.spec_from_file_location("ic_post_migrate_1_0_223", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("-at_install", "post_install")
class TestPersonalDefaultsArePanelAdministrators(_QueueCase):
    """Personal defaults on the panel's models, below and at Administrator."""

    def setUp(self):
        """A consultant of the project and an Administrator."""
        super().setUp()
        self.consultant = self._consultant("defaults-consultant")
        self.admin = new_test_user(
            self.env, login="defaults-admin",
            groups="base.group_user,base.group_system,"
            "incubacloud.group_cloud_manager",
        )
        self.Default = self.env["ir.default"]

    def _personal(self, model, field, value, user):
        """Save a personal default of *user* as the system does.

        :rtype: ir.default
        """
        self.Default.set(model, field, value, user_id=user.id)
        return self.Default.search([
            ("field_id.model", "=", model), ("field_id.name", "=", field),
            ("user_id", "=", user.id),
        ])

    def test_a_consultant_keeps_no_default_of_their_own_on_a_job(self):
        """Refused through ``set``, ``create`` and ``write``; elsewhere as before."""
        Default = self.Default.with_user(self.consultant)
        with self.assertRaisesRegex(AccessError, _ADMIN_ONLY), \
                self.env.cr.savepoint():
            Default.set(
                "cloud.job", "payload", {"move_rollback": True}, user_id=True,
            )
        field = self.env["ir.model.fields"]._get("cloud.job", "payload")
        with self.assertRaisesRegex(AccessError, _ADMIN_ONLY), \
                self.env.cr.savepoint():
            Default.create({
                "field_id": field.id, "user_id": self.consultant.id,
                "json_value": '{"move_rollback": true}',
            })
        saved = self._personal("cloud.job", "name", "saved", self.consultant)
        with self.assertRaisesRegex(AccessError, _ADMIN_ONLY), \
                self.env.cr.savepoint():
            saved.with_user(self.consultant).write({"json_value": '"changed"'})
        self.assertFalse(self.Default.search([
            ("field_id", "=", field.id), ("user_id", "=", self.consultant.id),
        ]))
        self.env.invalidate_all()
        self.assertEqual(saved.json_value, '"saved"')
        Default.set("res.partner", "comment", "mine", user_id=True)
        self.assertEqual(
            self.Default.with_user(self.consultant)._get_model_defaults(
                "res.partner",
            ).get("comment"),
            "mine",
        )

    def test_a_default_of_their_own_saved_before_is_not_applied(self):
        """``enqueue`` creates the job and its audit row with ``sudo``."""
        self._personal(
            "cloud.job", "payload", {"move_rollback": True}, self.consultant,
        )
        self._personal("cloud.audit.log", "user_id", self.admin.id, self.consultant)
        self._job("rebuild_instance")
        job = self.Job.browse(self.Job.with_user(self.consultant).enqueue(
            self.host.id, self.inst.id, "restart_instance",
        ))
        self.assertEqual(job.state, "queued")
        self.assertFalse(job.payload)
        audit = self.env["cloud.audit.log"].search([("job_id", "=", job.id)])
        self.assertEqual(audit.user_id, self.consultant)

    def test_an_administrator_sets_defaults(self):
        """For everyone, read by all; their own, read by them alone."""
        Default = self.Default.with_user(self.admin)
        Default.set("cloud.project", "apt_dependencies", "libfoo")
        Default.set("cloud.project", "pip_dependencies", "bar", user_id=True)
        self.assertEqual(
            self.Default.with_user(self.admin)._get_model_defaults("cloud.project"),
            {"apt_dependencies": "libfoo", "pip_dependencies": "bar"},
        )
        self.assertEqual(
            self.Default.with_user(self.consultant)._get_model_defaults(
                "cloud.project",
            ),
            {"apt_dependencies": "libfoo"},
        )

    def test_without_a_user_the_shared_defaults_are_read(self):
        """As a route with ``auth='none'`` reads them: nobody's own."""
        self.Default.set("cloud.project", "apt_dependencies", "libfoo")
        Default = api.Environment(self.env.cr, None, {})["ir.default"]
        self.assertFalse(Default._keeps_personal_panel_defaults())
        self.assertEqual(
            Default._get_model_defaults("cloud.project"),
            {"apt_dependencies": "libfoo"},
        )

    def test_the_migration_deletes_the_personal_defaults_of_the_panel(self):
        """Those of the panel's models only; the shared ones stay."""
        personal = self._personal(
            "cloud.job", "payload", {"move_rollback": True}, self.consultant,
        )
        own_partner = self._personal(
            "res.partner", "comment", "mine", self.consultant,
        )
        self.Default.set("cloud.project", "apt_dependencies", "libfoo")
        shared = self.Default.search([
            ("field_id.model", "=", "cloud.project"),
            ("field_id.name", "=", "apt_dependencies"),
            ("user_id", "=", False),
        ])
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, "19.0.1.0.222")
        self.env.invalidate_all()
        self.assertFalse(personal.exists())
        self.assertTrue(own_partner.exists())
        self.assertTrue(shared.exists())


@tagged("-at_install", "post_install")
class TestAnAuditRowTellsWhoActed(_QueueCase):
    """The defaults of the context do not name another user or date."""

    def test_a_consultant_queues_with_another_user_as_default(self):
        """The row of their job is theirs, written now."""
        consultant = self._consultant("audit-consultant")
        before = fields.Datetime.now()
        self._job("rebuild_instance")
        job = self.Job.browse(self.Job.with_user(consultant).with_context(
            default_user_id=self.env.ref("base.user_admin").id,
            default_create_date="2020-01-01 00:00:00",
            default_details="forged",
        ).enqueue(self.host.id, self.inst.id, "restart_instance"))
        audit = self.env["cloud.audit.log"].search([("job_id", "=", job.id)])
        self.assertEqual(audit.user_id, consultant)
        self.assertGreaterEqual(audit.create_date, before)
        self.assertFalse(audit.details)

    def test_a_manager_writing_a_row_names_nobody_else(self):
        """Their row is theirs, written now, whatever user or date it sends."""
        manager = new_test_user(
            self.env, login="audit-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        before = fields.Datetime.now()
        audit = self.env["cloud.audit.log"].with_user(manager).create({
            "action": "Config changed",
            "user_id": self.env.ref("base.user_admin").id,
            "create_date": "2020-01-01 00:00:00",
        })
        self.assertEqual(audit.user_id, manager)
        self.assertGreaterEqual(audit.create_date, before)

    def test_the_platform_names_the_user_it_acts_for(self):
        """Code with superuser rights still writes the user it passes."""
        other = new_test_user(self.env, login="audit-named")
        audit = self.env["cloud.audit.log"].sudo().create({
            "action": "Named", "user_id": other.id,
        })
        self.assertEqual(audit.user_id, other)


@tagged("-at_install", "post_install")
class TestTheContextOpensNoInternalPath(CloudSecurityBase):
    """Keys of the panel's own code, sent in an RPC call's context."""

    def test_a_consultant_does_not_write_the_lifecycle_state(self):
        """With ``cloud_state_transition`` or not; ``_transition`` still does."""
        consultant = self._create_user("state-consultant", "group_cloud_consultant")
        project = self._create_project("State", members=[consultant])
        inst = self._create_instance(project, state="deployed")
        for context in ({}, {"cloud_state_transition": True}):
            with self.subTest(context=context), self.assertRaises(UserError):
                inst.with_user(consultant).with_context(**context).write(
                    {"state": "draft"},
                )
        self.assertEqual(inst.state, "deployed")
        inst.with_user(consultant)._transition("deleting")
        self.assertEqual(inst.state, "deleting")

    def test_a_project_manager_does_not_choose_backups_by_default(self):
        """``default_backup_backend_id`` is checked as the value it is."""
        manager = self._create_user(
            "backups-project-manager", "group_cloud_project_manager",
        )
        backend = self.env["cloud.backup.backend"].create({
            "name": "defaults-backend",
            "s3_bucket": "defaults-bucket",
            "s3_access_key_id": "AKIADEFAULTS",
            "s3_secret_access_key": "secret",
        })
        with self.assertRaisesRegex(AccessError, "choose where backups"):
            self.env["cloud.project"].with_user(manager).with_context(
                default_backup_backend_id=backend.id,
            ).create({"name": "By default"})
        self.assertFalse(self.env["cloud.project"].search(
            [("name", "=", "By default")],
        ))

    def test_the_platforms_alerts_are_not_called_remotely(self):
        """``raise_alert`` and ``resolve_alert`` act as the platform."""
        Alert = self.env["cloud.alert"]
        for name in ("raise_alert", "resolve_alert"):
            with self.subTest(method=name), self.assertRaises(AccessError):
                get_public_method(Alert, name)


@tagged("-at_install", "post_install")
class TestTheEndpointGuardIsTheExecutors(KnownHostsCase):
    """``skip_endpoint_change_guard`` counts with superuser rights only."""

    def test_a_manager_sending_it_still_revokes_the_key(self):
        """The key is dropped as on any endpoint change."""
        manager = new_test_user(
            self.env, login="endpoint-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        host = self._host()
        host.with_user(manager).with_context(
            skip_endpoint_change_guard=True,
        ).write({"port": 22626})
        self.assertFalse(host.known_hosts_key)

    def test_the_executor_keeps_it(self):
        """With ``sudo``, as the hardening executor writes."""
        host = self._host()
        host.sudo().with_context(skip_endpoint_change_guard=True).write(
            {"port": 22626},
        )
        self.assertTrue(host.known_hosts_key)
