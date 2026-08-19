"""The docker-compose override must rotate every service's container log.

Odoo in a doodba stack logs to stdout, the copier template emits no
``logging:`` section, and Docker's ``json-file`` driver keeps growing
until something limits it. Only hosts that ran ``host_hardening`` carry
a ``daemon.json`` with ``log-opts`` — a Base-mode host set up by
``full_setup`` alone has none — so without a per-service limit the logs
fill the disk and every instance on the host goes down with Postgres.
The override the panel already writes for every deploy and rebuild is
where the guarantee belongs: per service, independent of the host.
"""
import yaml

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.registry import executor_registry


class _OverrideCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Logrot Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "logrot-host",
            "ip_address": "192.0.2.62",
            "user": "ubuntu",
            "wildcard_domain": "logrot.example.com",
        })
        # An SMTP relay makes production render three services, so the
        # assertion covers more than the two every environment shares.
        self.instance = self.env["cloud.instance"].create({
            "name": "logrotinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "smtp_relay_host": "smtp.example.com",
        })
        self.settings = self.env["cloud.settings"].sudo()._get()

    def _executor(self, executor_cls, job_type_code, inst=None):
        """Build *executor_cls* around a fresh job on *inst*."""
        inst = inst or self.instance
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", job_type_code)], limit=1,
        )
        self.assertTrue(job_type, f"job type {job_type_code} must exist")
        job = self.env["cloud.job"].create({
            "name": f"Logrot {job_type_code}",
            "host_id": self.host.id,
            "instance_id": inst.id,
            "job_type_id": job_type.id,
        })
        return executor_cls(job, self.host)

    def _override(self, executor):
        raw = executor._resource_override_content()
        self.assertTrue(raw, "override must never be None")
        return raw, yaml.safe_load(raw)

    def _assert_rotated(self, data, services, size="10m", files="3"):
        for svc in services:
            self.assertEqual(
                data["services"].get(svc, {}).get("logging"),
                {
                    "driver": "json-file",
                    "options": {"max-size": size, "max-file": files},
                },
                f"service {svc!r} must rotate its container log",
            )


class TestDeployOverrideLogging(_OverrideCase):

    def test_production_deploy_rotates_every_expected_service(self):
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        _raw, data = self._override(executor)
        expected = self.instance.expected_services()
        self.assertIn("smtp", expected)
        self.assertEqual(set(data["services"]), set(expected))
        self._assert_rotated(data, expected)

    def test_staging_deploy_rotates_the_test_services(self):
        staging = self.env["cloud.instance"].create({
            "name": "logrotstag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        executor = self._executor(
            DeployInstanceExecutor, "deploy_instance", inst=staging,
        )
        _raw, data = self._override(executor)
        self.assertEqual(
            set(data["services"]),
            set(DeployInstanceExecutor._TEST_SERVICES),
        )
        self._assert_rotated(data, DeployInstanceExecutor._TEST_SERVICES)

    def test_rebuild_rotates_every_expected_service(self):
        """The retrofit rides on the next rebuild, so rebuild must emit it."""
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        _raw, data = self._override(executor)
        self._assert_rotated(data, self.instance.expected_services())

    def test_limits_and_label_survive_alongside_logging(self):
        self.instance.write({"odoo_memory_limit": "1g", "odoo_cpus": 2.0})
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        _raw, data = self._override(executor)
        odoo = data["services"]["odoo"]
        self.assertEqual(odoo["mem_limit"], "1g")
        self.assertEqual(odoo["cpus"], 2.0)
        self.assertTrue(odoo.get("labels"))
        self._assert_rotated(data, self.instance.expected_services())

    def test_settings_drive_the_rotation_values(self):
        self.settings.write({
            "container_log_max_size": "50m",
            "container_log_max_file": 5,
        })
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        _raw, data = self._override(executor)
        self._assert_rotated(
            data, self.instance.expected_services(), size="50m", files="5",
        )

    def test_services_do_not_share_one_yaml_node(self):
        """PyYAML turns a dict referenced twice into an anchor + aliases.

        Compose would read them, but the file the operator finds on the
        host must be plain YAML they can read and edit by hand.
        """
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        raw, _data = self._override(executor)
        self.assertNotIn("&id", raw)
        self.assertNotIn("*id", raw)

    def test_rotation_settings_do_not_move_the_config_snapshot(self):
        """Global knobs are not per-instance drift.

        Changing the render of what a deploy ships has lit the whole
        fleet's "Changes not deployed" pill twice; the log options are
        read from settings at render time and must stay out of the
        hash, or bumping the retention would dirty every instance.
        """
        before = self.instance._config_snapshot_hash()
        self.settings.write({
            "container_log_max_size": "20m",
            "container_log_max_file": 2,
        })
        self.assertEqual(self.instance._config_snapshot_hash(), before)

    def test_every_registered_deploy_flavour_rotates(self):
        """Registry sweep: a new flavour that breaks super() fails here."""
        flavours = {
            code: cls
            for code, cls in executor_registry.all().items()
            if isinstance(cls, type)
            and issubclass(cls, DeployInstanceExecutor)
        }
        self.assertIn("deploy_instance", flavours)
        self.assertIn("rebuild_instance", flavours)
        for code, cls in flavours.items():
            with self.subTest(job_type=code, executor=cls.__name__):
                executor = self._executor(cls, code)
                _raw, data = self._override(executor)
                self._assert_rotated(data, list(data["services"]))


class TestContainerLogRotationSettings(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get()

    def test_defaults_match_the_host_hardening_daemon_json(self):
        """One criterion fleet-wide: 10m x 3, like host_hardening's log-opts."""
        self.assertEqual(self.settings.container_log_max_size, "10m")
        self.assertEqual(self.settings.container_log_max_file, 3)

    def test_accepts_kilo_and_giga_units(self):
        self.settings.write({"container_log_max_size": "100k"})
        self.settings.write({"container_log_max_size": "1g"})
        self.assertEqual(self.settings.container_log_max_size, "1g")

    def test_rejects_size_without_unit(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"container_log_max_size": "10"})

    def test_rejects_size_with_unknown_unit(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"container_log_max_size": "10x"})

    def test_rejects_zero_size(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"container_log_max_size": "0m"})

    def test_rejects_uppercase_unit(self):
        """Docker would take it, but one canonical spelling keeps the
        override diffable across the fleet; the UI lower-cases input."""
        with self.assertRaises(ValidationError):
            self.settings.write({"container_log_max_size": "10M"})

    def test_rejects_less_than_one_file(self):
        with self.assertRaises(ValidationError):
            self.settings.write({"container_log_max_file": 0})
