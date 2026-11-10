"""A production relay that signs in to Microsoft 365 with OAuth2.

Microsoft turns SMTP AUTH with passwords off by default at the end of
December 2026 (MC786329). An instance's relay can now sign in with the
client credentials of an app registered in Entra ID: the deploy runs the
``relay-oauth`` image, ships the credentials as a file of their own and
answers copier with Microsoft's server and the token file's path. Odoo
does not change. Pinned here: a relay with a password renders exactly as
before, the secret never leaves the panel in clear except into that
file, and a new secret reaches the running relay.
"""
import json
from unittest.mock import MagicMock, patch

import yaml

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.models import _relay_oauth
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    RELAY_OAUTH_IMAGE,
    SMTP_RELAY_IMAGE,
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)

DIRECTORY = "00000000-0000-4000-8000-0000000000aa"
CLIENT = "00000000-0000-4000-8000-0000000000bb"
SECRET = "relay-oauth-secret-7731"
MAILBOX = "notifications@example.com"


class _RelayOAuthCase(TransactionCase):
    """A production instance with a password relay, and helpers."""

    def setUp(self):
        """Create a host, a project and a production with a relay."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "roauth-host",
            "ip_address": "192.0.2.81",
            "user": "ubuntu",
            "wildcard_domain": "roauth.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "roauth"})
        self.inst = self.env["cloud.instance"].create({
            "name": "roauth-prod",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "smtp_relay_host": "smtp.example.com",
            "smtp_relay_port": 465,
            "smtp_relay_security": "ssl",
            "smtp_relay_user": "mailer@example.com",
            "smtp_relay_password": "relay-pass-1",
        })

    def _to_oauth(self, inst=None, **extra):
        """Switch *inst* (the production by default) to OAuth2."""
        inst = inst or self.inst
        inst.write({
            "smtp_relay_auth": _relay_oauth.AUTH_OAUTH2_MICROSOFT,
            "smtp_relay_oauth_directory_id": DIRECTORY,
            "smtp_relay_oauth_client_id": CLIENT,
            "smtp_relay_oauth_client_secret": SECRET,
            "smtp_relay_user": MAILBOX,
        } | extra)
        return inst

    def _executor(self, cls, code, inst=None):
        """Build *cls* around a fresh job of *code* on *inst*."""
        inst = inst or self.inst
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": code, "host_id": self.host.id,
            "instance_id": inst.id, "job_type_id": job_type.id,
        })
        return cls(job, self.host)

    def _smtp_override(self, cls=DeployInstanceExecutor, code="deploy_instance"):
        """Return the ``smtp`` entry of the override a job would write."""
        raw = self._executor(cls, code)._resource_override_content()
        return yaml.safe_load(raw)["services"]["smtp"]


class TestRelayOAuthModel(_RelayOAuthCase):

    def test_an_instance_signs_in_with_a_password_by_default(self):
        """A new column filled with its default: no data migration."""
        self.assertEqual(self.inst.smtp_relay_auth, "password")
        self.assertEqual(self.inst.smtp_relay_state, "unknown")
        self.env.cr.execute(
            "SELECT count(*) FROM cloud_instance WHERE smtp_relay_auth IS NULL"
        )
        self.assertEqual(self.env.cr.fetchone()[0], 0)

    def test_oauth2_sets_microsoft_s_server_and_keeps_the_password(self):
        self._to_oauth()
        self.assertEqual(self.inst.smtp_relay_host, "smtp.office365.com")
        self.assertEqual(self.inst.smtp_relay_port, 587)
        self.assertEqual(self.inst.smtp_relay_security, "starttls")
        # Kept: switching back to a password finds it.
        self.assertEqual(self.inst.smtp_relay_password, "relay-pass-1")
        # A host sent later is replaced by Microsoft's.
        self.inst.write({"smtp_relay_host": "smtp.example.com"})
        self.assertEqual(self.inst.smtp_relay_host, "smtp.office365.com")

    def test_an_instance_is_created_with_oauth2(self):
        inst = self.env["cloud.instance"].create({
            "name": "roauth-new",
            "project_id": self.env["cloud.project"].create({"name": "r2"}).id,
            "environment": "production",
            "host_id": self.host.id,
            "smtp_relay_auth": _relay_oauth.AUTH_OAUTH2_MICROSOFT,
            "smtp_relay_oauth_directory_id": "contoso.onmicrosoft.com",
            "smtp_relay_oauth_client_id": CLIENT,
            "smtp_relay_oauth_client_secret": SECRET,
            "smtp_relay_user": MAILBOX,
        })
        self.assertEqual(inst.smtp_relay_host, "smtp.office365.com")
        self.assertTrue(inst._relay_oauth_renders())

    def test_an_empty_secret_keeps_the_saved_one(self):
        self._to_oauth()
        self.inst.write({"smtp_relay_oauth_client_secret": ""})
        self.assertEqual(self.inst.smtp_relay_oauth_client_secret, SECRET)

    def test_staging_cannot_use_oauth2(self):
        staging = self.env["cloud.instance"].create({
            "name": "roauth-stag",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        with self.assertRaisesRegex(ValidationError, "production instances only"):
            self._to_oauth(staging)

    def test_what_oauth2_needs_is_checked(self):
        cases = {
            "smtp_relay_oauth_directory_id": ("dir id/../x", "Directory"),
            "smtp_relay_oauth_client_id": ("not-a-guid", "Application"),
            "smtp_relay_user": ("CORP\\mailer", "Sender mailbox"),
        }
        for field, (value, message) in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(
                ValidationError, message,
            ):
                with self.env.cr.savepoint():
                    self._to_oauth(**{field: value})

    def test_a_secret_is_required(self):
        with self.assertRaisesRegex(ValidationError, "Client secret"):
            self._to_oauth(smtp_relay_oauth_client_secret=False)

    def test_a_write_cannot_leave_oauth2_on_another_server(self):
        """Two instances, one per authentication: the host sent to both
        stays out of the OAuth2 one."""
        self._to_oauth()
        other = self.env["cloud.instance"].create({
            "name": "roauth-other",
            "project_id": self.env["cloud.project"].create({"name": "r3"}).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        with self.assertRaisesRegex(ValidationError, "cannot be changed"):
            (self.inst | other).write({"smtp_relay_host": "smtp.example.org"})

    def test_audit_tracks_the_choice_never_the_secret(self):
        tracked = self.inst._AUDIT_TRACKED_FIELDS
        self.assertIn("smtp_relay_auth", tracked)
        self.assertIn("smtp_relay_oauth_client_id", tracked)
        self.assertNotIn("smtp_relay_oauth_client_secret", tracked)

    def test_the_relay_state_is_the_platform_s(self):
        """Only the probe and the test mail write it, as the platform."""
        admin = self.env.ref("base.user_admin")
        with self.assertRaises(AccessError):
            self.inst.with_user(admin).write({"smtp_relay_state": "ok"})


class TestRelayOAuthRender(_RelayOAuthCase):

    def test_a_password_relay_renders_as_before(self):
        answers = self.inst._render_copier_answers()
        self.assertEqual(answers["smtp_relay_host"], "smtp.example.com")
        self.assertEqual(answers["smtp_relay_port"], 465)
        self.assertEqual(answers["smtp_relay_password"], "relay-pass-1")
        self.assertNotIn("smtp_relay_oauth", self.inst._render_config_snapshot())
        self.assertEqual(self.inst._extra_env_files(), {})
        smtp = self._smtp_override()
        self.assertEqual(smtp["image"], SMTP_RELAY_IMAGE)
        self.assertNotIn("volumes", smtp)
        self.assertNotIn(_relay_oauth.CREDENTIALS_LABEL, smtp["labels"])

    def test_oauth2_answers_microsoft_and_the_token_path(self):
        self._to_oauth()
        answers = self.inst._render_copier_answers()
        self.assertEqual(answers["smtp_relay_host"], "smtp.office365.com")
        self.assertEqual(answers["smtp_relay_port"], 587)
        self.assertEqual(answers["smtp_relay_user"], MAILBOX)
        self.assertEqual(answers["smtp_default_from"], MAILBOX)
        self.assertEqual(answers["smtp_relay_password"], _relay_oauth.TOKEN_PATH)
        self.assertNotIn(SECRET, json.dumps(answers))

    def test_the_snapshot_carries_a_salted_fingerprint_of_the_secret(self):
        self._to_oauth()
        snapshot = self.inst._render_config_snapshot()
        oauth = snapshot["smtp_relay_oauth"]
        self.assertEqual(
            set(oauth), {"provider", "directory_id", "client_id", "client_secret_hash"},
        )
        self.assertRegex(oauth["client_secret_hash"], r"^[0-9a-f]{16}$")
        self.assertNotIn(SECRET, json.dumps(snapshot, default=str))
        before = self.inst._config_snapshot_hash()
        self.inst.write({"smtp_relay_oauth_client_secret": "another-secret"})
        self.assertNotEqual(self.inst._config_snapshot_hash(), before)
        # The same secret on another instance fingerprints differently.
        other = self.env["cloud.instance"].create({
            "name": "roauth-copy",
            "project_id": self.env["cloud.project"].create({"name": "r4"}).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self._to_oauth(other, smtp_relay_oauth_client_secret="another-secret")
        self.assertNotEqual(
            other._relay_oauth_snapshot()["client_secret_hash"],
            self.inst._relay_oauth_snapshot()["client_secret_hash"],
        )

    def test_the_credentials_file_holds_what_the_renewer_reads(self):
        self._to_oauth()
        files = self.inst._extra_env_files()
        self.assertEqual(list(files), [".docker/smtp-oauth.json"])
        self.assertEqual(json.loads(files[".docker/smtp-oauth.json"]), {
            "provider": "microsoft",
            _relay_oauth.CREDENTIALS_DIRECTORY_KEY: DIRECTORY,
            "client_id": CLIENT,
            "client_secret": SECRET,
            "mailbox": MAILBOX,
        })

    def test_the_override_runs_the_oauth2_image_with_its_credentials(self):
        self._to_oauth()
        for cls, code in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            with self.subTest(job_type=code):
                smtp = self._smtp_override(cls, code)
                self.assertEqual(smtp["image"], RELAY_OAUTH_IMAGE)
                self.assertEqual(smtp["volumes"], [
                    "./.docker/smtp-oauth.json:/run/secrets/ic-relay-oauth.json:ro",
                ])
                self.assertEqual(smtp["environment"], {"ENABLE_UPDATE_CHECK": "0"})
                self.assertIn("incubacloud.protect", smtp["labels"])
                self.assertNotIn(SECRET, yaml.safe_dump(smtp))

    def test_a_new_secret_recreates_the_relay(self):
        """Compose recreates a container whose labels changed; the content
        of a bind-mounted file is not part of its configuration."""
        self._to_oauth()
        before = self._smtp_override()["labels"][_relay_oauth.CREDENTIALS_LABEL]
        self.inst.write({"smtp_relay_oauth_client_secret": "rotated-secret"})
        after = self._smtp_override()["labels"][_relay_oauth.CREDENTIALS_LABEL]
        self.assertNotEqual(before, after)
        self.assertRegex(after, r"^[0-9a-f]{16}$")

    def test_the_oauth2_image_is_built_on_the_password_relay_s_release(self):
        """One docker-mailserver for both relays (RB-31): the image's tag
        starts with SMTP_RELAY_IMAGE's release, and it is pinned by digest."""
        release = SMTP_RELAY_IMAGE.rsplit(":", 1)[1]
        self.assertRegex(
            RELAY_OAUTH_IMAGE,
            r"^ghcr\.io/incubacloud/relay-oauth:"
            + release.replace(".", r"\.")
            + r"-\d+\.\d+-\d+@sha256:[0-9a-f]{64}$",
        )

    def test_without_oauth2_deploy_and_rebuild_remove_the_credentials(self):
        """A file that holds a secret has no business on the host once
        OAuth2 is off; nothing else would remove it."""
        d = self.inst.get_remote_dir()
        for cls, code in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            with self.subTest(job_type=code):
                commands = dict(
                    c[:2] for c in self._executor(cls, code).get_commands()
                )
                self.assertEqual(
                    commands["Remove the relay credentials"],
                    f"rm -rf {d}/.docker/smtp-oauth.json",
                )

    def test_with_oauth2_the_file_is_written_where_docker_left_no_directory(self):
        """Docker turns a bind-mounted file missing at ``up`` into a
        directory; ``mv`` would put the new file inside it."""
        self._to_oauth()
        d = self.inst.get_remote_dir()
        for cls, code in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            with self.subTest(job_type=code):
                labels = [c[0] for c in self._executor(cls, code).get_commands()]
                commands = dict(
                    c[:2] for c in self._executor(cls, code).get_commands()
                )
                path = f"{d}/.docker/smtp-oauth.json"
                self.assertEqual(
                    commands["Clear the relay credentials path"],
                    f"if [ -d {path} ]; then rm -rf {path}; fi",
                )
                self.assertLess(
                    labels.index("Clear the relay credentials path"),
                    labels.index("Write extra env files"),
                )
                self.assertLess(
                    labels.index("Write extra env files"),
                    labels.index("Write compose override"),
                )
                self.assertNotIn(SECRET, repr(commands))


class TestRelayOAuthRoutes(_RelayOAuthCase):

    def _call(self, method, **kwargs):
        """Drive a data-load route against the test cursor."""
        from odoo.addons.incubacloud.controllers import data_load
        from odoo.addons.incubacloud.controllers._data_load import _routes_crud

        fake_request = MagicMock(spec=http.Request)
        fake_request.env = self.env
        with (
            patch.object(http, "request", fake_request),
            patch.object(data_load, "request", fake_request),
            patch.object(_routes_crud, "request", fake_request),
        ):
            controller = data_load.CloudDataLoadController()
            return getattr(controller, method)(**kwargs)

    def test_the_form_saves_oauth2_and_never_reads_the_secret_back(self):
        answer = self._call("cloud_save_instance", instance_id=self.inst.id, vals={
            "smtp_relay_auth": _relay_oauth.AUTH_OAUTH2_MICROSOFT,
            "smtp_relay_oauth_directory_id": DIRECTORY,
            "smtp_relay_oauth_client_id": CLIENT,
            "smtp_relay_oauth_client_secret": SECRET,
            "smtp_relay_user": MAILBOX,
        })
        self.assertTrue(answer.get("ok"), answer)
        self.assertTrue(self.inst._relay_oauth_renders())
        page = self._call("cloud_get_instance", instance_id=self.inst.id)
        self.assertEqual(page["smtp_relay_auth"], "oauth2_microsoft")
        self.assertEqual(page["smtp_relay_oauth_directory_id"], DIRECTORY)
        self.assertEqual(page["smtp_relay_oauth_client_id"], CLIENT)
        self.assertTrue(page["has_smtp_relay_oauth_client_secret"])
        self.assertEqual(page["smtp_relay_state"], "unknown")
        self.assertNotIn(SECRET, repr(page))

    def test_get_secret_does_not_reveal_it(self):
        self._to_oauth()
        answer = self._call(
            "cloud_get_secret", model="cloud.instance",
            record_id=self.inst.id, field="smtp_relay_oauth_client_secret",
        )
        self.assertEqual(answer, {"ok": False, "error": "Field not allowed"})


@tagged("post_install", "-at_install")
class TestRelayOAuthSecretAccess(_RelayOAuthCase):
    """A developer saves the client secret and nobody but the system reads it.

    ``post_install``: the users need a partner, and a module installed
    later may add a column to it.
    """

    def setUp(self):
        """A developer and a consultant of the project."""
        super().setUp()
        self.developer = new_test_user(
            self.env, login="roauth-developer",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )
        self.consultant = new_test_user(
            self.env, login="roauth-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        self.project.member_ids = [(4, self.consultant.id), (4, self.developer.id)]

    _call = TestRelayOAuthRoutes._call

    def test_a_developer_saves_it_in_the_form_and_cannot_read_it(self):
        self.env = self.env(user=self.developer)
        answer = self._call("cloud_save_instance", instance_id=self.inst.id, vals={
            "smtp_relay_auth": _relay_oauth.AUTH_OAUTH2_MICROSOFT,
            "smtp_relay_oauth_directory_id": DIRECTORY,
            "smtp_relay_oauth_client_id": CLIENT,
            "smtp_relay_oauth_client_secret": SECRET,
            "smtp_relay_user": MAILBOX,
        })
        self.assertTrue(answer.get("ok"), answer)
        page = self._call("cloud_get_instance", instance_id=self.inst.id)
        self.assertTrue(page["has_smtp_relay_oauth_client_secret"])
        self.assertNotIn(SECRET, repr(page))
        inst = self.inst.with_user(self.developer)
        with self.assertRaises(AccessError):
            inst.read(["smtp_relay_oauth_client_secret"])
        with self.assertRaises(AccessError):
            inst.search_read(
                [("id", "=", inst.id)], ["smtp_relay_oauth_client_secret"],
            )
        self.assertEqual(self.inst.sudo()._relay_oauth_secret(), SECRET)
        files = self.inst.with_user(self.developer)._extra_env_files()
        self.assertIn(SECRET, files[_relay_oauth.CREDENTIALS_FILE])

    def test_without_one_saved_the_form_says_so(self):
        self.env = self.env(user=self.developer)
        page = self._call("cloud_get_instance", instance_id=self.inst.id)
        self.assertFalse(page["has_smtp_relay_oauth_client_secret"])

    def test_a_consultant_cannot_write_it(self):
        with self.assertRaises(AccessError):
            self.inst.with_user(self.consultant).write(
                {"smtp_relay_oauth_client_secret": "another"},
            )
