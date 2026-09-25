"""Who may read a staging's mail, and what happens when it cannot be read.

The mailbox holds password resets, invitations and customer addresses
belonging to whoever owns the database the staging was copied from, so
the three endpoints are gated exactly like the logs — developer and up,
capped per user — and never store what they read.

The failures matter as much as the gate. A catcher that is not running,
a stopped staging and a production instance all produce "no mail", and
an operator who cannot tell those apart concludes the feature is broken.
Each one answers in its own words, and these tests hold them to it.
"""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_security_mixin import (
    CloudSecurityMixin,
)
from odoo.addons.incubacloud.controllers import _rate_limit
from odoo.addons.incubacloud.controllers._data_load import _routes_ops
from odoo.addons.incubacloud.models.cloud_rate_limit import (
    RATE_LIMIT_DEFAULTS,
)

#: What the host's projection prints for a mailbox with one message.
_ONE_MESSAGE = json.dumps({
    "ok": True,
    "total": 1,
    "items": [{
        "id": "abc=@mailhog.example",
        "created": "2026-09-23T08:19:09.261059533Z",
        "from": "noreply@staging.example.com",
        "to": ["someone@example.org"],
        "subject": "Restablecer contraseña",
        "size": 997,
        "attachments": 0,
    }],
})


class _MailboxCase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "mb-host",
            "ip_address": "10.0.16.1",
            "user": "root",
            "wildcard_domain": "mb.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "mb-proj"})
        self.staging = self.env["cloud.instance"].create({
            "name": "mb-staging",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self._deploy(self.staging)
        self.controller = _routes_ops.OpsMixin()
        self._allow_everything()

    def _deploy(self, instance):
        """Put *instance* in the state a Mails tab needs: up and running.

        ``deployed`` is derived from the lifecycle state and refuses a
        direct write, so the state machine is walked rather than
        short-circuited — a test that sets a state no flow produces
        proves nothing about the flow.
        """
        instance._transition("deployed")
        instance.sudo().write({"running": True})

    def _allow_everything(self):
        """Grant the role, so each test below isolates one other thing."""
        sec = MagicMock(spec=CloudSecurityMixin)
        sec._check_can_view_logs.return_value = True
        self.controller._sec = lambda: sec
        self.sec = sec

    def _request(self):
        """A request bound to this test's env, as the controllers expect."""
        request = MagicMock(spec=Request)
        request.env = self.env
        return request

    def _call(self, method, *args, stdout="", stderr="", raises=None,
              **kwargs):
        """Invoke one route with the SSH hop replaced.

        :param method: name of the controller method
        :param stdout: what the host would have printed
        :param stderr: what the host would have printed on stderr
        :param raises: exception the SSH call raises instead
        :return: the route's response dict
        """
        if raises is not None:
            ssh = MagicMock(side_effect=raises)
        else:
            ssh = MagicMock(return_value=(stdout, stderr))
        self.controller._ssh_run = ssh
        with self._patched_request():
            result = getattr(self.controller, method)(*args, **kwargs)
        self.ssh = ssh
        return result

    @contextmanager
    def _patched_request(self):
        """Bind ``request`` in both modules the routes go through.

        Two patches, not one: ``_rate_limit`` imports ``request`` into
        its own namespace, so patching only the route module leaves the
        gate reaching for a request that is not bound.
        """
        fake = self._request()
        with patch.object(_routes_ops, "request", fake), \
                patch.object(_rate_limit, "request", fake):
            yield


class TestTheGate(_MailboxCase):
    """Developer and up, the same floor as the logs."""

    def test_every_endpoint_checks_the_role(self):
        for method, args in (
            ("cloud_instance_mailbox", (self.staging.id,)),
            ("cloud_instance_mail", (self.staging.id, "a=@mailhog")),
            ("cloud_instance_mailbox_clear", (self.staging.id,)),
        ):
            self.sec.reset_mock()
            self._call(method, *args, stdout=_ONE_MESSAGE)
            self.sec._check_can_view_logs.assert_called_once()

    def test_a_refused_role_stops_the_route(self):
        self.sec._check_can_view_logs.side_effect = AccessError("nope")
        with self.assertRaises(AccessError):
            self._call("cloud_instance_mailbox", self.staging.id)

    def test_nothing_is_read_when_the_role_is_refused(self):
        """The gate comes before the host is touched, not after."""
        self.sec._check_can_view_logs.side_effect = AccessError("nope")
        ssh = MagicMock(return_value=("", ""))
        self.controller._ssh_run = ssh
        with self._patched_request():
            with self.assertRaises(AccessError):
                self.controller.cloud_instance_mailbox(self.staging.id)
        ssh.assert_not_called()


class TestWhichInstancesHaveAMailbox(_MailboxCase):
    """Four ways to have no mail, four different sentences."""

    def test_production_says_its_mail_is_real(self):
        prod = self.env["cloud.instance"].create({
            "name": "mb-prod",
            "project_id": self.env["cloud.project"].create(
                {"name": "mb-proj-2"},
            ).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self._deploy(prod)
        res = self._call("cloud_instance_mailbox", prod.id)
        self.assertFalse(res["ok"])
        self.assertIn("real email", res["error"])

    def test_an_undeployed_staging_says_so(self):
        self.staging._transition("deleting")
        self.staging._transition("draft")
        res = self._call("cloud_instance_mailbox", self.staging.id)
        self.assertFalse(res["ok"])
        self.assertIn("not been deployed", res["error"])

    def test_a_stopped_staging_says_so(self):
        self.staging.sudo().write({"running": False})
        res = self._call("cloud_instance_mailbox", self.staging.id)
        self.assertFalse(res["ok"])
        self.assertIn("stopped", res["error"])

    def test_an_instance_with_no_host_reads_as_undeployed(self):
        """It has no stack, which is what "not deployed" means.

        Reported as "not found" it would say the record vanished, which
        is a different thing and sends the reader looking elsewhere.
        """
        self.staging.sudo().write({"host_id": False})
        res = self._call("cloud_instance_mailbox", self.staging.id)
        self.assertFalse(res["ok"])
        self.assertIn("not been deployed", res["error"])

    def test_a_missing_instance_says_so(self):
        res = self._call("cloud_instance_mailbox", 0)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_none_of_those_touch_the_host(self):
        """Refusing costs an SSH connection if the order is wrong."""
        self.staging.sudo().write({"running": False})
        self._call("cloud_instance_mailbox", self.staging.id)
        self.ssh.assert_not_called()


class TestReadingTheMailbox(_MailboxCase):

    def test_a_listing_is_returned_as_the_host_projected_it(self):
        res = self._call(
            "cloud_instance_mailbox", self.staging.id, stdout=_ONE_MESSAGE,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], 1)
        self.assertEqual(
            res["items"][0]["subject"], "Restablecer contraseña",
        )

    def test_the_command_reaches_the_instances_own_host(self):
        self._call(
            "cloud_instance_mailbox", self.staging.id, stdout=_ONE_MESSAGE,
        )
        host, command = self.ssh.call_args.args
        self.assertEqual(host, self.host)
        self.assertIn("api/v2/messages", command)

    def test_a_catcher_that_answers_nothing_is_reported_as_itself(self):
        """Empty stdout means the pipeline broke, not an empty mailbox."""
        res = self._call("cloud_instance_mailbox", self.staging.id, stdout="")
        self.assertFalse(res["ok"])
        self.assertIn("did not answer", res["error"])

    def test_html_from_a_broken_proxy_is_not_parsed_as_a_mailbox(self):
        res = self._call(
            "cloud_instance_mailbox", self.staging.id,
            stdout="<html>502 Bad Gateway</html>",
        )
        self.assertFalse(res["ok"])
        self.assertIn("did not answer", res["error"])

    def test_an_ssh_failure_is_surfaced_without_its_internals(self):
        res = self._call(
            "cloud_instance_mailbox", self.staging.id,
            raises=OSError("Connection refused by 10.0.16.1 port 22"),
        )
        self.assertFalse(res["ok"])
        self.assertNotIn("10.0.16.1", json.dumps(res))

    def test_an_empty_mailbox_is_a_success(self):
        res = self._call(
            "cloud_instance_mailbox", self.staging.id,
            stdout=json.dumps({"ok": True, "total": 0, "items": []}),
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["items"], [])


class TestReadingOneMessage(_MailboxCase):

    def test_a_message_is_returned_decoded(self):
        payload = json.dumps({
            "ok": True, "id": "abc=@mailhog.example",
            "subject": "Contraseña", "text": "Hola", "html": "",
            "attachments": [], "headers": [],
        })
        res = self._call(
            "cloud_instance_mail", self.staging.id, "abc=@mailhog.example",
            stdout=payload,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["subject"], "Contraseña")

    def test_an_id_shaped_like_a_shell_command_is_refused(self):
        res = self._call(
            "cloud_instance_mail", self.staging.id, "a@b; rm -rf /",
            stdout=_ONE_MESSAGE,
        )
        self.assertFalse(res["ok"])
        self.assertIn("Invalid message id", res["error"])

    def test_a_refused_id_never_reaches_the_host(self):
        self._call(
            "cloud_instance_mail", self.staging.id, "a@b`id`",
            stdout=_ONE_MESSAGE,
        )
        self.ssh.assert_not_called()


class TestClearingTheMailbox(_MailboxCase):

    def test_a_confirmed_clear_reports_success(self):
        res = self._call(
            "cloud_instance_mailbox_clear", self.staging.id,
            stdout="IC_MAILBOX_CLEARED\n",
        )
        self.assertTrue(res["ok"])

    def test_a_clear_without_its_marker_is_a_failure(self):
        """MailHog answers a delete with nothing, so silence proves nothing."""
        res = self._call(
            "cloud_instance_mailbox_clear", self.staging.id, stdout="",
        )
        self.assertFalse(res["ok"])
        self.assertIn("did not answer", res["error"])

    def test_clearing_is_written_to_the_audit_log(self):
        """Deleting somebody's mail leaves a trace of who did it."""
        self._call(
            "cloud_instance_mailbox_clear", self.staging.id,
            stdout="IC_MAILBOX_CLEARED\n",
        )
        rows = self.env["cloud.audit.log"].search([
            ("instance_id", "=", self.staging.id),
            ("action", "=", "Cleared staging mailbox"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.user_id, self.env.user)

    def test_a_failed_clear_is_not_audited_as_done(self):
        self._call("cloud_instance_mailbox_clear", self.staging.id, stdout="")
        rows = self.env["cloud.audit.log"].search([
            ("instance_id", "=", self.staging.id),
            ("action", "=", "Cleared staging mailbox"),
        ])
        self.assertFalse(rows)


class TestTheCap(_MailboxCase):
    """Its own bucket and its own setting, not the log one."""

    def test_the_rule_reads_the_mail_setting(self):
        with self._patched_request():
            rule = self.controller._mailbox_rule()
        self.assertEqual(rule.cap_key, "rate_limit_mail_reads_per_min")

    def test_the_bucket_is_per_user(self):
        with self._patched_request():
            rule = self.controller._mailbox_rule()
        self.assertIn(str(self.env.uid), rule.bucket)
        self.assertTrue(rule.bucket.startswith("mailbox_user:"))

    def test_the_setting_has_a_documented_default(self):
        settings = self.env["cloud.settings"].sudo()._get()
        self.assertEqual(settings.rate_limit_mail_reads_per_min, 30)
        self.assertEqual(
            RATE_LIMIT_DEFAULTS["rate_limit_mail_reads_per_min"], 30,
        )
