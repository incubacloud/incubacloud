"""Secrets no role of the panel reads through Odoo's generic endpoints.

Odoo answers ``read``, ``search_read``, the export and a search for every
field the caller's groups allow (``/web/dataset/call_kw``). These fields
carried no group, or one that every operator of a host has: a Stakeholder
read a Manager's Telegram bot token, a Developer a host's dashboard
password and a Manager the central's operator credential. They carry
``base.group_system`` now, and the panel's own code reads them elevated,
so what uses them keeps working for the roles it is meant for.
"""

from unittest.mock import MagicMock, patch

from odoo import http
from odoo.api import call_kw
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged

#: The four roles of the panel, by the group each one is given.
ROLES = {
    "stakeholder": "incubacloud.group_cloud_user",
    "consultant": "incubacloud.group_cloud_consultant",
    "developer": "incubacloud.group_cloud_developer",
    "manager": "incubacloud.group_cloud_manager",
}


class _SecretFieldCase(TransactionCase):
    """One user per role, and the checks every secret field must pass."""

    @classmethod
    def setUpClass(cls):
        """Give optional NOT NULL partner columns a default, as the
        security suite does, so the role users can be created."""
        super().setUpClass()
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if "char" in dtype or "text" in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner ALTER COLUMN "{col}" '
                f"SET DEFAULT {default}"
            )

    def setUp(self):
        """Create one user per role, each allowed to export."""
        super().setUp()
        self.roles = {
            role: new_test_user(
                self.env,
                login=f"secret_groups_{role}",
                groups=f"base.group_user,{group},base.group_allow_export",
            )
            for role, group in ROLES.items()
        }

    def _assert_unreadable(self, records, fnames, users):
        """Assert that none of *users* reads any of *fnames* of *records*.

        Neither by ``read``, ``search_read`` (directly or through
        ``call_kw``), the export nor a search on the field, and the field
        is not even described to them.

        :param records: the records holding the secrets
        :param fnames: the names of the secret fields
        :param users: the users who must not read them
        """
        ids = records.ids
        for user in users:
            Model = records.with_user(user).browse()
            for fname in fnames:
                with self.subTest(user=user.login, field=fname):
                    with self.assertRaises(AccessError):
                        Model.browse(ids).read([fname])
                    with self.assertRaises(AccessError):
                        Model.search_read([("id", "in", ids)], [fname])
                    with self.assertRaises(AccessError):
                        call_kw(Model, "read", [ids, [fname]], {})
                    with self.assertRaises(AccessError):
                        call_kw(
                            Model, "search_read", [],
                            {"domain": [("id", "in", ids)], "fields": [fname]},
                        )
                    with self.assertRaises(AccessError):
                        Model.browse(ids).export_data([fname])
                    with self.assertRaises(AccessError):
                        Model.search([(fname, "!=", False)])
                    self.assertNotIn(fname, Model.fields_get())

    def _call(self, controller, method, user, **kwargs):
        """Drive a route of *controller* against the test cursor as *user*.

        :param controller: the controller instance whose route is called
        :param str method: the route's method name
        :param user: the user the request is made as
        :return: what the route answers
        """
        from odoo.addons.incubacloud.controllers import connect, data_load
        from odoo.addons.incubacloud.controllers._data_load import (
            _routes_crud,
        )

        fake_request = MagicMock(spec=http.Request)
        fake_request.env = self.env(user=user)
        with (
            patch.object(http, "request", fake_request),
            patch.object(data_load, "request", fake_request),
            patch.object(_routes_crud, "request", fake_request),
            patch.object(connect, "request", fake_request),
        ):
            return getattr(controller, method)(**kwargs)


@tagged("post_install", "-at_install")
class TestUserNotificationSecrets(_SecretFieldCase):
    """A user's Telegram bot token and webhook signing secret.

    ``res.users`` is readable by every internal user, so the two fields
    were too. Their owner sets them in the panel's notification
    preferences, which only say whether each is set, and the senders of
    jobs and alerts read them elevated.
    """

    FIELDS = ("cloud_telegram_bot_token", "cloud_webhook_secret")

    def setUp(self):
        """Give the Manager both secrets and an endpoint for each."""
        super().setUp()
        self.manager = self.roles["manager"]
        self.manager.write({
            "cloud_notification_level": "all",
            "cloud_telegram_bot_token": "123456:manager-bot-token",
            "cloud_telegram_chat_id": "424242",
            "cloud_webhook_url": "https://hooks.example.com/manager",
            "cloud_webhook_secret": "manager-signing-secret",
        })
        # Nobody else is told: the admin of the database may have its own.
        self.env["res.users"].browse(2).write({
            "cloud_telegram_bot_token": False,
            "cloud_telegram_chat_id": False,
            "cloud_webhook_url": False,
        })
        self.host = self.env["cloud.host"].create({
            "name": "secret-groups-notify-host",
            "ip_address": "198.51.100.61",
            "user": "root",
            "wildcard_domain": "secret-groups-notify.example.com",
        })

    def test_no_role_reads_another_user_s_secrets(self):
        """A Stakeholder no longer reads the Manager's token in clear."""
        others = [self.roles[r] for r in ("stakeholder", "consultant", "developer")]
        other_manager = new_test_user(
            self.env, login="secret_groups_manager_2",
            groups="base.group_user,incubacloud.group_cloud_manager,"
                   "base.group_allow_export",
        )
        self._assert_unreadable(self.manager, self.FIELDS, others + [other_manager])

    def test_no_role_reads_its_own_secrets_through_the_orm(self):
        """Not even their owner: the preferences say whether one is set."""
        for user in self.roles.values():
            user.write({"cloud_telegram_bot_token": f"own-{user.login}"})
            self._assert_unreadable(user, self.FIELDS, [user])

    def test_the_owner_sets_them_and_is_told_they_are_set(self):
        """The preferences store both and answer only that they exist."""
        from odoo.addons.incubacloud.controllers import connect

        controller = connect.InstanceConnectController()
        developer = self.roles["developer"]
        answer = self._call(
            controller, "save_user_preferences", developer,
            cloud_notification_level="all",
            cloud_notification_mode="immediate",
            cloud_telegram_bot_token="987654:developer-bot-token",
            cloud_webhook_secret="developer-signing-secret",
        )
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(
            developer._get_cloud_telegram_bot_token(), "987654:developer-bot-token",
        )
        self.assertEqual(
            developer._get_cloud_webhook_secret(), "developer-signing-secret",
        )
        prefs = self._call(controller, "get_user_preferences", developer)
        self.assertTrue(prefs["cloud_telegram_configured"])
        self.assertTrue(prefs["cloud_webhook_configured"])
        self.assertNotIn("developer-bot-token", repr(prefs))
        self.assertNotIn("developer-signing-secret", repr(prefs))

    def test_the_owner_s_test_message_uses_the_token(self):
        """The preferences' test button reaches the bot it names."""
        from odoo.addons.incubacloud.controllers import connect

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch.object(
            connect.urllib.request, "urlopen", autospec=True,
            return_value=response,
        ) as urlopen:
            answer = self._call(
                connect.InstanceConnectController(), "telegram_send_test",
                self.manager,
            )
        self.assertEqual(answer, {"ok": True})
        self.assertIn("/bot123456:manager-bot-token/", urlopen.call_args[0][0].full_url)

    def test_a_job_s_notification_reads_them_for_whoever_ran_it(self):
        """Jobs end in the environment of the Developer who queued them."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        ) or self.env["cloud.job.type"].create(
            {"name": "host_probe", "code": "host_probe", "apply_to": "host"},
        )
        job = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": "Secret groups probe",
            "queue_job_uuid": "uuid-secret-groups",
        })
        Job = self.env["cloud.job"].with_user(self.roles["developer"])
        with (
            patch(
                "odoo.addons.incubacloud.models.cloud_job.safe_urlopen",
                autospec=True,
            ) as telegram,
            patch(
                "odoo.addons.incubacloud.models.cloud_job.post_json",
                autospec=True,
            ) as webhook,
        ):
            Job.with_context(test_external_notify=True)._notify_external(
                job, "failed",
            )
        self.assertIn(
            "/bot123456:manager-bot-token/", telegram.call_args[0][0].full_url,
        )
        self.assertIn(
            "X-IncubaCloud-Signature", webhook.call_args.kwargs["headers"],
        )

    def test_an_alert_s_notification_reads_them_for_whoever_raised_it(self):
        """``_send_alert_telegram`` and ``_send_alert_webhook``, elevated."""
        alert = self.env["cloud.alert"].sudo().create({
            "code": "secret_groups_alert",
            "message": "Secret groups alert",
            "level": "critical",
            "host_id": self.host.id,
        })
        alert = alert.with_user(self.roles["developer"]).with_context(
            test_external_notify=True,
        )
        with (
            patch(
                "odoo.addons.incubacloud.models.cloud_alert.safe_urlopen",
                autospec=True,
            ) as telegram,
            patch(
                "odoo.addons.incubacloud.models.cloud_alert.post_json",
                autospec=True,
            ) as webhook,
        ):
            alert._notify_alert_external()
        self.assertIn(
            "/bot123456:manager-bot-token/", telegram.call_args[0][0].full_url,
        )
        self.assertIn(
            "X-IncubaCloud-Signature", webhook.call_args.kwargs["headers"],
        )


@tagged("post_install", "-at_install")
class TestTraefikPanelPassword(_SecretFieldCase):
    """A host's Traefik dashboard password, as its SSH password.

    It carried the Developer group, so a Developer read it through the
    ORM while ``/cloud/get_secret`` only revealed it to a Manager. It is
    for system administrators now; the setup a Developer queues still
    ships it, and the Manager still reveals it on the host page.
    """

    def setUp(self):
        """Create a host with a known dashboard password."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "secret-groups-traefik-host",
            "ip_address": "198.51.100.62",
            "user": "root",
            "wildcard_domain": "secret-groups-traefik.example.com",
            "traefik_panel_password": "dashboard-pass-6271",
        })

    def _controller(self):
        """Return the controller that serves the host routes."""
        from odoo.addons.incubacloud.controllers import data_load

        return data_load.CloudDataLoadController()

    def test_no_role_reads_the_password(self):
        """A Developer and a Manager no longer read it through the ORM."""
        self._assert_unreadable(
            self.host, ["traefik_panel_password"], list(self.roles.values()),
        )

    def test_a_developer_s_setup_still_ships_it(self):
        """What Full Setup uploads, rendered as the Developer who queued it.

        Watched where the password goes in: a layer above may take the
        dashboard's public router, and its hash with it, out of the file.
        """
        from odoo.addons.incubacloud.models import full_setup_executor

        host = self.host.with_user(self.roles["developer"])
        with patch.object(
            full_setup_executor, "_build_inverseproxy",
            wraps=full_setup_executor._build_inverseproxy,
        ) as build:
            host._shipped_inverseproxy_yaml()
        self.assertEqual(build.call_args.args[2], "dashboard-pass-6271")

    def test_the_manager_reveals_it_and_the_developer_does_not(self):
        """``/cloud/get_secret`` keeps its rule: write access to the host."""
        answer = self._call(
            self._controller(), "cloud_get_secret", self.roles["manager"],
            model="cloud.host", record_id=self.host.id,
            field="traefik_panel_password",
        )
        self.assertEqual(answer, {"value": "dashboard-pass-6271"})
        with self.assertRaises(AccessError):
            self._call(
                self._controller(), "cloud_get_secret", self.roles["developer"],
                model="cloud.host", record_id=self.host.id,
                field="traefik_panel_password",
            )

    def test_the_manager_changes_it_and_the_page_says_it_is_set(self):
        """The host page's save stores it; its flag reads it elevated."""
        manager = self.roles["manager"]
        self._call(
            self._controller(), "cloud_save_host", manager,
            host_id=self.host.id, vals={"traefik_panel_password": "new-pass-8812"},
        )
        self.assertEqual(self.host.traefik_panel_password, "new-pass-8812")
        page = self._call(
            self._controller(), "cloud_get_host", manager, host_id=self.host.id,
        )
        self.assertTrue(page["has_traefik_panel_password"])
        self.assertNotIn("new-pass-8812", repr(page))
        # Flushed first: the cache would otherwise write the new password
        # back over the column cleared below.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_host SET traefik_panel_password = '' WHERE id = %s",
            (self.host.id,),
        )
        self.host.invalidate_recordset(["traefik_panel_password"])
        page = self._call(
            self._controller(), "cloud_get_host", manager, host_id=self.host.id,
        )
        self.assertFalse(page["has_traefik_panel_password"])

    def test_a_manager_creates_a_host_with_its_own_password(self):
        """``create`` stores it elevated, as the SSH password."""
        host = self.env["cloud.host"].with_user(self.roles["manager"]).create({
            "name": "secret-groups-traefik-new",
            "ip_address": "198.51.100.63",
            "user": "root",
            "wildcard_domain": "secret-groups-traefik-new.example.com",
            "traefik_panel_password": "chosen-pass-1203",
        })
        self.assertEqual(host.sudo().traefik_panel_password, "chosen-pass-1203")


@tagged("post_install", "-at_install")
class TestMetricsCentralSecrets(_SecretFieldCase):
    """The central's credentials on ``cloud.settings``.

    Readable by every Manager through the ORM, although only the panel's
    code uses them: the account's write token, the operator's unfiltered
    read credential and VictoriaMetrics' deletion key, which only the
    gateway may present.
    """

    FIELDS = (
        "metrics_remote_write_token",
        "metrics_operator_token",
        "metrics_delete_auth_key",
    )

    def setUp(self):
        """Store a value in each of the three secrets."""
        super().setUp()
        self.settings = self.env["cloud.settings"]._get_system()
        self.settings.write({
            "metrics_account": "acct_secretgroups",
            "metrics_remote_write_token": "write-token-5501",
            "metrics_operator_token": "operator-token-5503",
            "metrics_delete_auth_key": "delete-key-5504",
        })

    def _controller(self):
        """Return the controller that serves the settings routes."""
        from odoo.addons.incubacloud.controllers import data_load

        return data_load.CloudDataLoadController()

    def test_no_role_reads_the_central_s_secrets(self):
        """Not even a Manager, who edits the rest of the settings."""
        self._assert_unreadable(
            self.settings, self.FIELDS, [self.roles["manager"]],
        )

    def test_the_panel_s_code_still_reads_them_for_a_manager(self):
        """What talks to the central reads them elevated."""
        Settings = self.env["cloud.settings"].with_user(self.roles["manager"])
        self.assertEqual(
            Settings._metrics_auth(), ("acct_secretgroups", "write-token-5501"),
        )
        self.assertEqual(
            Settings._metrics_auth(operator=True),
            ("operator", "operator-token-5503"),
        )
        self.assertEqual(Settings._ensure_metrics_delete_key(), "delete-key-5504")

    def test_the_settings_page_says_the_token_is_set_and_stores_a_new_one(self):
        """The page learns only that it is set, and may still replace it."""
        manager = self.roles["manager"]
        page = self._call(self._controller(), "cloud_get_general_settings", manager)
        self.assertTrue(page["has_metrics_remote_write_token"])
        for value in (
            "write-token-5501", "operator-token-5503", "delete-key-5504",
        ):
            self.assertNotIn(value, repr(page))
        answer = self._call(
            self._controller(), "cloud_save_general_settings", manager,
            metrics_remote_write_token="write-token-7702",
        )
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(
            self.settings._metrics_auth(), ("acct_secretgroups", "write-token-7702"),
        )
