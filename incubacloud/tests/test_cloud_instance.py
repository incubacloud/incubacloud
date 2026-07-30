"""
Tier 2 — ORM integration tests for cloud.instance.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestCloudInstancePasswords(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Test Project"})

    def _create_instance(self, **kw):
        base = {"name": "test", "project_id": self.project.id, "environment": "staging"}
        return self.env["cloud.instance"].create(base | kw)

    def test_create_auto_generates_admin_password(self):
        inst = self._create_instance()
        self.assertTrue(inst.odoo_admin_password)

    def test_create_auto_generates_db_password(self):
        inst = self._create_instance()
        self.assertTrue(inst.postgres_password)

    def test_create_explicit_admin_password_preserved(self):
        inst = self._create_instance(odoo_admin_password="my-admin-pwd")
        self.assertEqual(inst.odoo_admin_password, "my-admin-pwd")

    def test_create_explicit_db_password_preserved(self):
        inst = self._create_instance(postgres_password="my-db-pwd")
        self.assertEqual(inst.postgres_password, "my-db-pwd")

    def test_write_empty_admin_password_preserves_existing(self):
        inst = self._create_instance()
        original = inst.odoo_admin_password
        inst.write({"odoo_admin_password": ""})
        self.assertEqual(inst.odoo_admin_password, original)

    def test_write_empty_db_password_preserves_existing(self):
        inst = self._create_instance()
        original = inst.postgres_password
        inst.write({"postgres_password": ""})
        self.assertEqual(inst.postgres_password, original)

    def test_write_empty_smtp_password_preserves_existing(self):
        inst = self._create_instance(smtp_relay_password="smtp-pass")
        inst.write({"smtp_relay_password": ""})
        self.assertEqual(inst.smtp_relay_password, "smtp-pass")

    def test_write_new_password_updates(self):
        inst = self._create_instance()
        inst.write({"odoo_admin_password": "new-pass"})
        self.assertEqual(inst.odoo_admin_password, "new-pass")

    def test_create_auto_generates_admin_user_password(self):
        inst = self._create_instance()
        self.assertTrue(inst.odoo_admin_user_password)

    def test_create_explicit_admin_user_password_preserved(self):
        inst = self._create_instance(odoo_admin_user_password="user-pwd")
        self.assertEqual(inst.odoo_admin_user_password, "user-pwd")

    def test_write_empty_admin_user_password_preserves_existing(self):
        inst = self._create_instance()
        original = inst.odoo_admin_user_password
        inst.write({"odoo_admin_user_password": ""})
        self.assertEqual(inst.odoo_admin_user_password, original)

    def test_admin_password_and_user_password_are_different(self):
        """Master password and login password must be distinct auto-generated values."""
        inst = self._create_instance()
        self.assertNotEqual(inst.odoo_admin_password, inst.odoo_admin_user_password)

    def test_two_instances_get_different_passwords(self):
        inst1 = self._create_instance(name="inst1")
        inst2 = self._create_instance(name="inst2")
        self.assertNotEqual(inst1.odoo_admin_password, inst2.odoo_admin_password)
        self.assertNotEqual(inst1.postgres_password, inst2.postgres_password)


class TestCloudInstanceDependencies(TransactionCase):
    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create(
            {
                "name": "Dep Project",
                "pip_dependencies": "requests\nflask\n",
                "apt_dependencies": "libpq-dev\n",
            }
        )

    def _create_instance(self, **kw):
        return self.env["cloud.instance"].create(
            {"name": "depinst", "project_id": self.project.id, "environment": "staging"}
            | kw
        )

    def test_create_copies_pip_deps_from_project(self):
        inst = self._create_instance()
        self.assertEqual(inst.pip_dependencies, "requests\nflask\n")

    def test_create_copies_apt_deps_from_project(self):
        inst = self._create_instance()
        self.assertEqual(inst.apt_dependencies, "libpq-dev\n")

    def test_create_explicit_pip_deps_not_overwritten(self):
        inst = self._create_instance(pip_dependencies="my-dep\n")
        self.assertEqual(inst.pip_dependencies, "my-dep\n")

    def test_create_explicit_apt_deps_not_overwritten(self):
        inst = self._create_instance(apt_dependencies="curl\n")
        self.assertEqual(inst.apt_dependencies, "curl\n")


class TestCloudInstanceEffectiveBackupBackend(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Backend = self.env["cloud.backup.backend"]
        self.project = self.env["cloud.project"].create({"name": "Backup Project"})
        # Clear any global param that might affect results
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", ""
        )

    def _create_backend(self, name):
        return self.Backend.create(
            {"name": name, "backend_type": "s3", "s3_bucket": name}
        )

    def _create_instance(self, **kw):
        return self.env["cloud.instance"].create(
            {
                "name": "backup-test",
                "project_id": self.project.id,
                "environment": "staging",
            }
            | kw
        )

    def test_instance_level_overrides_project(self):
        inst_b = self._create_backend("instance-backend")
        proj_b = self._create_backend("project-backend")
        self.project.write({"backup_backend_id": proj_b.id})
        inst = self._create_instance(backup_backend_id=inst_b.id)
        self.assertEqual(inst.effective_backup_backend, inst_b)

    def test_project_level_fallback(self):
        proj_b = self._create_backend("project-backend")
        self.project.write({"backup_backend_id": proj_b.id})
        inst = self._create_instance()
        self.assertEqual(inst.effective_backup_backend, proj_b)

    def test_global_fallback(self):
        global_b = self._create_backend("global-backend")
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", str(global_b.id)
        )
        inst = self._create_instance()
        self.assertEqual(inst.effective_backup_backend, global_b)

    def test_no_backend_gives_empty(self):
        inst = self._create_instance()
        self.assertFalse(inst.effective_backup_backend)

    def test_instance_backend_overrides_global(self):
        inst_b = self._create_backend("my-backend")
        global_b = self._create_backend("global-backend")
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", str(global_b.id)
        )
        inst = self._create_instance(backup_backend_id=inst_b.id)
        self.assertEqual(inst.effective_backup_backend, inst_b)


class TestCloudInstanceHealthCron(TransactionCase):
    """Tests for cron_instance_health() scoping."""

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Health Project"})
        self.host = self.env["cloud.host"].create(
            {
                "name": "Health Host",
                "ip_address": "10.0.0.5",
                "user": "ubuntu",
                "wildcard_domain": "test.example.com",
                "user": "ubuntu",
            }
        )
        jt = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1
        )
        if not jt:
            jt = self.env["cloud.job.type"].create(
                {
                    "name": "Instance Health",
                    "code": "instance_health",
                    "apply_to": "instance",
                }
            )
        self.job_type = jt

    def _create_instance(self, deployed=True, with_host=True, **kw):
        base = {
            "name": "health-inst",
            "project_id": self.project.id,
            "environment": "staging",
            "state": "deployed" if deployed else "draft",
        }
        if with_host:
            base["host_id"] = self.host.id
        return self.env["cloud.instance"].create(base | kw)

    def _patch_enqueue(self, side_effect):
        """Patch cloud.job.enqueue with the given side_effect callable.

        ``delattr`` on the class after the test restores MRO lookup so
        downstream modules (SaaS) see the correct override instead of a
        stale own-attribute.
        """
        Model = self.env["cloud.job"]
        model_class = type(Model)
        model_class.enqueue = side_effect
        self.addCleanup(delattr, model_class, "enqueue")

    def test_deployed_instance_with_host_gets_job(self):
        inst = self._create_instance(deployed=True)
        calls = []

        def _mock(self_model, host_id, instance_id, job_type_code, payload=None):
            calls.append((host_id, instance_id, job_type_code))
            return 0

        self._patch_enqueue(side_effect=_mock)
        self.env["cloud.instance"].cron_instance_health()
        self.assertTrue(
            any(c[1] == inst.id and c[2] == "instance_health" for c in calls)
        )

    def test_non_deployed_instance_skipped(self):
        inst = self._create_instance(deployed=False)
        calls = []

        def _mock(self_model, host_id, instance_id, job_type_code, payload=None):
            calls.append((host_id, instance_id, job_type_code))
            return 0

        self._patch_enqueue(side_effect=_mock)
        self.env["cloud.instance"].cron_instance_health()
        self.assertFalse(any(c[1] == inst.id for c in calls))

    def test_instance_without_host_skipped(self):
        inst = self._create_instance(deployed=True, with_host=False)
        calls = []

        def _mock(self_model, host_id, instance_id, job_type_code, payload=None):
            calls.append((host_id, instance_id, job_type_code))
            return 0

        self._patch_enqueue(side_effect=_mock)
        self.env["cloud.instance"].cron_instance_health()
        self.assertFalse(any(c[1] == inst.id for c in calls))

    def test_last_health_check_field_writable(self):
        from odoo import fields as odoo_fields

        inst = self._create_instance()
        now = odoo_fields.Datetime.now()
        inst.write({"last_health_check": now})
        self.assertEqual(inst.last_health_check, now)


class TestCloudInstanceOdooVersion(TransactionCase):
    """Test odoo_version as Selection field."""

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Test Project"})

    def _create_instance(self, **kw):
        base = {
            "name": "test-ver",
            "project_id": self.project.id,
            "environment": "staging",
        }
        return self.env["cloud.instance"].create(base | kw)

    def test_default_version_is_19(self):
        inst = self._create_instance()
        self.assertEqual(inst.odoo_version, "19.0")

    def test_explicit_version_preserved(self):
        inst = self._create_instance(odoo_version="16.0")
        self.assertEqual(inst.odoo_version, "16.0")

    def test_version_is_string_not_float(self):
        inst = self._create_instance(odoo_version="18.0")
        self.assertIsInstance(inst.odoo_version, str)

    def test_invalid_version_raises(self):
        with self.assertRaises(Exception):
            self._create_instance(odoo_version="99.0")


# ---------------------------------------------------------------------------
# Check backup backend validation
# ---------------------------------------------------------------------------


class TestCheckBackupBackend(TransactionCase):
    """_check_backup_backend blocks production actions without backend."""

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "BB Test"})
        self.host = self.env["cloud.host"].create(
            {
                "name": "bb-host",
                "ip_address": "10.0.0.99",
                "user": "root",
                "wildcard_domain": "test.io",
            }
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            "",
        )

    def _create_instance(self, **kw):
        base = {
            "name": "bb-inst",
            "project_id": self.project.id,
            "host_id": self.host.id,
            "environment": "production",
        }
        return self.env["cloud.instance"].create(base | kw)

    def _create_backend(self):
        return self.env["cloud.backup.backend"].create(
            {
                "name": "test-bb",
                "backend_type": "s3",
                "s3_bucket": "test-bucket",
            }
        )

    def test_deploy_already_deployed_raises(self):
        inst = self._create_instance(state="deployed")
        with self.assertRaises(UserError) as ctx:
            inst.deploy()
        self.assertIn("already deployed", str(ctx.exception))

    def test_deploy_production_no_backend_raises(self):
        inst = self._create_instance()
        with self.assertRaises(UserError) as ctx:
            inst.deploy()
        self.assertIn("Settings", str(ctx.exception))

    def test_deploy_production_with_backend_ok(self):
        bb = self._create_backend()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            str(bb.id),
        )
        inst = self._create_instance()
        # deploy() will try to enqueue a job — it may fail for other
        # reasons (no queue_job channel, etc.) but should NOT raise
        # the backup-backend UserError.
        try:
            inst.deploy()
        except UserError as e:
            if "backup" in str(e).lower():
                self.fail(f"Should not raise backup error: {e}")

    def test_deploy_staging_no_backend_ok(self):
        inst = self._create_instance(environment="staging")
        # Staging does not require backup backend
        try:
            inst.deploy()
        except UserError as e:
            if "backup" in str(e).lower():
                self.fail(f"Should not raise backup error: {e}")

    def test_list_backups_no_backend_raises(self):
        inst = self._create_instance()
        with self.assertRaises(UserError) as ctx:
            inst.list_backups()
        self.assertIn("Settings", str(ctx.exception))

    def test_create_backup_no_backend_raises(self):
        inst = self._create_instance(state="deployed")
        with self.assertRaises(UserError) as ctx:
            inst.create_backup()
        self.assertIn("Settings", str(ctx.exception))

    def test_error_message_mentions_all_levels(self):
        inst = self._create_instance()
        with self.assertRaises(UserError) as ctx:
            inst.deploy()
        msg = str(ctx.exception)
        self.assertIn("instance", msg)
        self.assertIn("project", msg)
        self.assertIn("Settings", msg)


# ---------------------------------------------------------------------------
# Default backup backend settings (ir.config_parameter)
# ---------------------------------------------------------------------------


class TestDefaultBackupBackendSetting(TransactionCase):
    """Global default backup backend via ir.config_parameter."""

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.param = "incubacloud.backup_backend_id"
        self.ICP.set_param(self.param, "")

    def test_default_is_empty(self):
        val = int(self.ICP.get_param(self.param, 0) or 0)
        self.assertFalse(val)

    def test_set_and_read(self):
        bb = self.env["cloud.backup.backend"].create(
            {
                "name": "global-bb",
                "backend_type": "s3",
                "s3_bucket": "global-bucket",
            }
        )
        self.ICP.set_param(self.param, str(bb.id))
        val = int(self.ICP.get_param(self.param, 0) or 0)
        self.assertEqual(val, bb.id)

    def test_clear_resets_to_zero(self):
        self.ICP.set_param(self.param, "42")
        self.ICP.set_param(self.param, "0")
        val = int(self.ICP.get_param(self.param, 0) or 0)
        self.assertFalse(val)

    def test_effective_backend_uses_global(self):
        bb = self.env["cloud.backup.backend"].create(
            {
                "name": "global-bb",
                "backend_type": "s3",
                "s3_bucket": "global-bucket",
            }
        )
        self.ICP.set_param(self.param, str(bb.id))
        project = self.env["cloud.project"].create({"name": "GP"})
        inst = self.env["cloud.instance"].create(
            {
                "name": "gi",
                "project_id": project.id,
                "environment": "production",
            }
        )
        self.assertEqual(inst.effective_backup_backend, bb)


# ---------------------------------------------------------------------------
# cron_refresh_backup_list — compose-service gating
# ---------------------------------------------------------------------------


class TestCronRefreshBackupList(TransactionCase):
    """``cron_refresh_backup_list`` must skip instances whose compose
    stack has no ``backup`` service (e.g. free-tier tenants that inherit
    the global backup backend but never ship a backup container)."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "bk-host",
                "ip_address": "10.0.0.10",
                "user": "ubuntu",
                "wildcard_domain": "test.example.com",
            }
        )
        self.project = self.env["cloud.project"].create({"name": "BK Project"})
        bb = self.env["cloud.backup.backend"].create(
            {
                "name": "cron-bb",
                "backend_type": "s3",
                "s3_bucket": "cron-bucket",
            }
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            str(bb.id),
        )

    def _create_instance(self, name, compose_services, **kw):
        base = {
            "name": name,
            "project_id": self.project.id,
            "host_id": self.host.id,
            "environment": "production",
            "state": "deployed",
            "compose_services": compose_services,
        }
        return self.env["cloud.instance"].create(base | kw)

    def _patch_list_backups(self):
        """Patch ``cloud.instance.list_backups`` and return the call log."""
        calls = []
        Model = self.env["cloud.instance"]
        original = type(Model).list_backups

        def _mock(self_model):
            for rec in self_model:
                calls.append(rec.id)
            return 0

        type(Model).list_backups = _mock
        return calls, original, type(Model)

    def _restore_list_backups(self, original, model_class):
        model_class.list_backups = original

    def test_instance_with_backup_service_gets_enqueued(self):
        inst = self._create_instance("paid-prod", "odoo,db,backup,smtp")
        calls, original, mc = self._patch_list_backups()
        try:
            self.env["cloud.instance"].cron_refresh_backup_list()
        finally:
            self._restore_list_backups(original, mc)
        self.assertIn(inst.id, calls)

    def test_instance_without_backup_service_skipped(self):
        """Free-tier instance: backup backend inherited but no container."""
        inst = self._create_instance("free-prod", "odoo,db,smtp")
        calls, original, mc = self._patch_list_backups()
        try:
            self.env["cloud.instance"].cron_refresh_backup_list()
        finally:
            self._restore_list_backups(original, mc)
        self.assertNotIn(inst.id, calls)

    def test_instance_with_empty_compose_services_skipped(self):
        inst = self._create_instance("blank-prod", "")
        calls, original, mc = self._patch_list_backups()
        try:
            self.env["cloud.instance"].cron_refresh_backup_list()
        finally:
            self._restore_list_backups(original, mc)
        self.assertNotIn(inst.id, calls)

    def test_compose_services_with_whitespace_recognized(self):
        """Ensure trimming handles a YAML-formatted list with spaces."""
        inst = self._create_instance(
            "spaced-prod",
            "odoo, db, backup , smtp",
        )
        calls, original, mc = self._patch_list_backups()
        try:
            self.env["cloud.instance"].cron_refresh_backup_list()
        finally:
            self._restore_list_backups(original, mc)
        self.assertIn(inst.id, calls)

    def test_staging_instance_skipped(self):
        inst = self._create_instance(
            "stg",
            "odoo,db,backup",
            environment="staging",
        )
        calls, original, mc = self._patch_list_backups()
        try:
            self.env["cloud.instance"].cron_refresh_backup_list()
        finally:
            self._restore_list_backups(original, mc)
        self.assertNotIn(inst.id, calls)

    def test_move_in_flight_skipped(self):
        """Instances with move_origin_host_id set are excluded from
        the backup list refresh (the stack may be down during a move
        and the backup container unreachable)."""
        inst = self._create_instance(
            "moving",
            "odoo,db,backup",
        )
        source = self.env["cloud.host"].create(
            {
                "name": "src2",
                "ip_address": "10.0.0.11",
                "user": "ubuntu",
                "wildcard_domain": "src2.example.com",
            }
        )
        inst.move_origin_host_id = source.id
        calls, original, mc = self._patch_list_backups()
        try:
            self.env["cloud.instance"].cron_refresh_backup_list()
        finally:
            self._restore_list_backups(original, mc)
        self.assertNotIn(inst.id, calls)


class TestAutoDomainWildcard(TransactionCase):
    """Auto-generated instance domains substitute the wildcard label.

    ``cloud.host.wildcard_domain`` is stored in wildcard form
    (``*.example.com``); a concrete hostname must replace the ``*`` label
    with the instance subdomain, not prepend to the whole wildcard (which
    would yield an invalid ``sub.*.example.com``)."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "wc-host",
                "ip_address": "10.0.0.20",
                "user": "ubuntu",
                "wildcard_domain": "*.18.incubacloud.io",
            }
        )
        self.project = self.env["cloud.project"].create({"name": "WC Project"})

    def test_subdomain_suffix_strips_wildcard(self):
        self.assertEqual(self.host._subdomain_suffix(), "18.incubacloud.io")

    def test_subdomain_suffix_plain_domain_unchanged(self):
        self.host.wildcard_domain = "plain.example.com"
        self.assertEqual(self.host._subdomain_suffix(), "plain.example.com")

    def test_auto_domain_on_create_substitutes_wildcard(self):
        inst = self.env["cloud.instance"].create(
            {
                "name": "web",
                "project_id": self.project.id,
                "host_id": self.host.id,
            }
        )
        self.assertEqual(len(inst.domain_ids), 1)
        hostname = inst.domain_ids.hostname
        self.assertNotIn("*", hostname)
        self.assertTrue(hostname.endswith(".18.incubacloud.io"), hostname)

    def test_auto_domain_on_host_assign_substitutes_wildcard(self):
        inst = self.env["cloud.instance"].create(
            {
                "name": "api",
                "project_id": self.project.id,
            }
        )
        self.assertFalse(inst.domain_ids)
        inst.write({"host_id": self.host.id})
        self.assertEqual(len(inst.domain_ids), 1)
        hostname = inst.domain_ids.hostname
        self.assertNotIn("*", hostname)


class TestCloudInstanceRemoteDirUniquePerHost(TransactionCase):
    """Two instances resolving to the same remote directory on the same
    host would clobber each other's compose files, DB volume and
    filestore. Uses ``custom_remote_dir`` (the imported-instance override)
    to force a directory collision independent of ``project_id``/``name``
    — matching those instead would also collide on the auto-generated DNS
    hostname, which is a separate, already-enforced constraint."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "Shared Host",
                "ip_address": "10.0.0.30",
                "user": "ubuntu",
                "wildcard_domain": "example.com",
            }
        )
        self.proj_a = self.env["cloud.project"].create({"name": "Project A"})
        self.proj_b = self.env["cloud.project"].create({"name": "Project B"})

    def test_same_host_same_custom_dir_raises(self):
        self.env["cloud.instance"].create(
            {
                "name": "app1",
                "project_id": self.proj_a.id,
                "host_id": self.host.id,
                "custom_remote_dir": "~/imported/shared-dir",
            }
        )
        with self.assertRaises(ValidationError):
            self.env["cloud.instance"].create(
                {
                    "name": "app2",
                    "project_id": self.proj_b.id,
                    "host_id": self.host.id,
                    "custom_remote_dir": "~/imported/shared-dir",
                }
            )

    def test_same_custom_dir_different_host_ok(self):
        other_host = self.env["cloud.host"].create(
            {
                "name": "Other Host",
                "ip_address": "10.0.0.31",
                "user": "ubuntu",
                "wildcard_domain": "other.example.com",
            }
        )
        self.env["cloud.instance"].create(
            {
                "name": "app1",
                "project_id": self.proj_a.id,
                "host_id": self.host.id,
                "custom_remote_dir": "~/imported/shared-dir",
            }
        )
        # Should not raise — different host, no real collision.
        self.env["cloud.instance"].create(
            {
                "name": "app2",
                "project_id": self.proj_b.id,
                "host_id": other_host.id,
                "custom_remote_dir": "~/imported/shared-dir",
            }
        )

    def test_no_host_assigned_skips_check(self):
        self.env["cloud.instance"].create(
            {
                "name": "app1",
                "project_id": self.proj_a.id,
                "custom_remote_dir": "~/imported/shared-dir",
            }
        )
        # Should not raise — neither instance has a host yet.
        self.env["cloud.instance"].create(
            {
                "name": "app2",
                "project_id": self.proj_b.id,
                "custom_remote_dir": "~/imported/shared-dir",
            }
        )


class TestCloudInstanceUnlinkGuard(TransactionCase):
    """unlink() must reject a deployed instance and force callers through
    the delete_instance job path instead of orphaning a live container."""

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Unlink Test"})

    def _create_instance(self, **kw):
        base = {
            "name": "unlink-test",
            "project_id": self.project.id,
            "environment": "staging",
        }
        return self.env["cloud.instance"].create(base | kw)

    def test_unlink_deployed_raises(self):
        inst = self._create_instance(state="deployed")
        with self.assertRaises(UserError) as ctx:
            inst.unlink()
        self.assertIn("still deployed", str(ctx.exception))
        self.assertTrue(inst.exists())

    def test_unlink_not_deployed_ok(self):
        inst = self._create_instance()
        inst.unlink()
        self.assertFalse(inst.exists())

    def test_finalize_removal_keep_in_panel(self):
        inst = self._create_instance(state="deployed", running=True)
        inst._finalize_removal(keep_in_panel=True)
        self.assertFalse(inst.deployed)
        self.assertFalse(inst.running)
        self.assertTrue(inst.exists())

    def test_finalize_removal_delete_completely(self):
        inst = self._create_instance(state="deployed", running=True)
        inst._finalize_removal(keep_in_panel=False)
        self.assertFalse(inst.exists())


class TestCronCheckProductionBackup(TransactionCase):
    """Imprescindible #9b — production instance without backup alert."""

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id",
            "0",
        )
        self.project = self.env["cloud.project"].create({"name": "NB-proj"})
        self.bb = self.env["cloud.backup.backend"].create(
            {
                "name": "NB-bb",
                "backend_type": "s3",
                "s3_bucket": "nb-bucket",
            }
        )

    def _alert(self, inst):
        return self.env["cloud.alert"].search(
            [
                ("code", "=", "production_no_backup"),
                ("instance_id", "=", inst.id),
                ("state", "=", "active"),
            ]
        )

    def _instance(self, **kw):
        base = {
            "name": "nb-inst",
            "project_id": self.project.id,
            "environment": "production",
            "state": "deployed",
        }
        return self.env["cloud.instance"].create(base | kw)

    def test_deployed_production_without_backend_flagged(self):
        inst = self._instance()
        result = self.env["cloud.instance"].cron_check_production_backup()
        self.assertEqual(result["flagged"], 1)
        self.assertTrue(self._alert(inst))

    def test_deployed_production_with_backend_not_flagged(self):
        inst = self._instance(backup_backend_id=self.bb.id)
        result = self.env["cloud.instance"].cron_check_production_backup()
        self.assertEqual(result["flagged"], 0)
        self.assertFalse(self._alert(inst))

    def test_staging_instance_never_flagged(self):
        self.env["cloud.instance"].create(
            {
                "name": "nb-staging",
                "project_id": self.project.id,
                "environment": "staging",
                "state": "deployed",
            }
        )
        result = self.env["cloud.instance"].cron_check_production_backup()
        self.assertEqual(result["flagged"], 0)

    def test_undeployed_production_never_flagged(self):
        self._instance(state="draft")
        result = self.env["cloud.instance"].cron_check_production_backup()
        self.assertEqual(result["flagged"], 0)

    def test_assigning_backend_resolves_alert(self):
        inst = self._instance()
        self.env["cloud.instance"].cron_check_production_backup()
        self.assertTrue(self._alert(inst))
        inst.backup_backend_id = self.bb.id
        self.env["cloud.instance"].cron_check_production_backup()
        self.assertFalse(self._alert(inst))
