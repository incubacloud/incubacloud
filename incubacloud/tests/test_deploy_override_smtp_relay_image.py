"""The production ``smtp`` relay runs a docker-mailserver release the panel chose.

The copier answers say ``smtp_relay_version: latest`` and every deploy
runs ``docker compose pull``, so the tag the template renders moves with
upstream: docker-mailserver v17.0.0 became ``latest`` on 2026-11-05, and
from then on each new deploy, rebuild or move started v17 while the
instances deployed before kept the release they had pulled. The override
the panel writes on every deploy names the image instead; the answers are
left alone, since a new value there would change the configuration hash
of every instance in the fleet.
"""
import json

from odoo.addons.incubacloud.models.deploy_instance_executor import (
    SMTP_RELAY_IMAGE,
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.registry import executor_registry

from .test_deploy_override_smtp_update_check import _SmtpOverrideCase


class TestSmtpRelayImagePinned(_SmtpOverrideCase):

    def test_pin_names_a_release_not_a_moving_tag(self):
        """``latest`` or a bare major (``16``) would move with upstream."""
        self.assertRegex(
            SMTP_RELAY_IMAGE,
            r"^ghcr\.io/docker-mailserver/docker-mailserver:\d+\.\d+\.\d+$",
        )
        self.assertEqual(
            SMTP_RELAY_IMAGE,
            "ghcr.io/docker-mailserver/docker-mailserver:16.0.1",
        )

    def test_deploy_pins_the_relay_image(self):
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance")
        )
        self.assertEqual(data["services"]["smtp"].get("image"), SMTP_RELAY_IMAGE)

    def test_rebuild_pins_the_relay_image(self):
        """``copier update`` renders ``latest`` again on every rebuild."""
        _raw, data = self._override(
            self._executor(RebuildInstanceExecutor, "rebuild_instance")
        )
        self.assertEqual(data["services"]["smtp"].get("image"), SMTP_RELAY_IMAGE)

    def test_update_check_switch_stays(self):
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance")
        )
        self.assertEqual(
            data["services"]["smtp"]["environment"],
            {"ENABLE_UPDATE_CHECK": "0"},
        )

    def test_staging_mail_catcher_keeps_its_image(self):
        """A staging's ``smtp`` is MailHog: a docker-mailserver image there
        would replace the catcher with a relay that sends for real."""
        staging = self.env["cloud.instance"].create({
            "name": "relaypinstag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
            "smtp_relay_host": "smtp.example.com",
        })
        _raw, data = self._override(
            self._executor(
                DeployInstanceExecutor, "deploy_instance", inst=staging,
            )
        )
        self.assertIn("smtp", data["services"])
        self.assertNotIn("image", data["services"]["smtp"])

    def test_no_other_service_gets_an_image(self):
        _raw, data = self._override(
            self._executor(DeployInstanceExecutor, "deploy_instance")
        )
        for svc, entry in data["services"].items():
            if svc == "smtp":
                continue
            self.assertNotIn("image", entry, f"service {svc!r}")

    def test_config_snapshot_is_unchanged_by_the_pin(self):
        """The pin lives in the override, outside the hash: no instance
        shows changes to apply, and each takes it at its next deploy."""
        snapshot = json.dumps(
            self.instance._render_config_snapshot(), sort_keys=True,
        )
        self.assertNotIn("docker-mailserver", snapshot)

    def test_every_registered_deploy_flavour_pins_it(self):
        """Registry sweep: a flavour that breaks super() fails here."""
        flavours = {
            code: cls
            for code, cls in executor_registry.all().items()
            if isinstance(cls, type)
            and issubclass(cls, DeployInstanceExecutor)
        }
        for code, cls in flavours.items():
            with self.subTest(job_type=code, executor=cls.__name__):
                _raw, data = self._override(self._executor(cls, code))
                if "smtp" not in data["services"]:
                    continue
                self.assertEqual(
                    data["services"]["smtp"].get("image"), SMTP_RELAY_IMAGE,
                )
