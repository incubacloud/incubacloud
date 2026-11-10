"""Whether a production's mail relay delivers, and its test mail.

The health probe reads the relay's queue, its OAuth2 token and the last
mail it delivered, and raises ``smtp_relay_failing`` (critical) when the
relay does not hand mail to its server, whatever the cause and whatever
the sign-in. Only a delivery resolves it. "Send test mail" sends one mail
to whoever asks and records the server's answer as evidence at once.

The ``delay_reason`` texts are Postfix 3.10's in docker-mailserver
16.0.1, read in the spike of 2026-11-08; the 535 5.7.139 is Microsoft
365's own, read on 2026-11-09.
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models import _relay_oauth
from odoo.addons.incubacloud.models.instance_health_executor import (
    InstanceHealthExecutor,
)
from odoo.addons.incubacloud.models.relay_test_mail_executor import (
    RelayTestMailExecutor,
)

HOST = "smtp.example.com"
REFUSED = f"connect to {HOST}[192.0.2.5]:587: Connection refused"
TIMED_OUT = f"connect to {HOST}[192.0.2.5]:587: Connection timed out"
DNS = (
    "Host or domain name not found. Name service error for "
    f"name={HOST} type=A: Host not found"
)
NO_STARTTLS = f"TLS is required, but was not offered by host {HOST}[192.0.2.5]"
TLS_BROKEN = "Cannot start TLS: handshake failure"
AUTH_535 = (
    "SASL authentication failed; server smtp.office365.com[52.0.0.1] said: "
    "535 5.7.3 Authentication unsuccessful"
)
AUTH_139 = (
    "SASL authentication failed; server smtp.office365.com[52.0.0.1] said: "
    "535 5.7.139 Authentication unsuccessful, SmtpClientAuthentication is "
    "disabled. Visit https://aka.ms/smtp_auth_disabled for more information."
)
NO_TOKEN = (
    "SASL authentication failed; cannot authenticate to server "
    "smtp.office365.com[52.0.0.1]: generic failure"
)
RCPT = (
    f"host {HOST}[192.0.2.5] said: 550 5.1.1 <someone@example.org>: "
    "Recipient address rejected (in reply to RCPT TO command)"
)


class TestClassification(BaseCase):
    """Each cause the spike recorded, by its Postfix text."""

    def test_each_cause_is_named(self):
        cases = {
            REFUSED: "connection_refused",
            TIMED_OUT: "connection_timeout",
            DNS: "dns",
            NO_STARTTLS: "no_starttls",
            TLS_BROKEN: "tls_failed",
            AUTH_139: "smtp_auth_disabled",
            NO_TOKEN: "no_token",
            "host h said: 554 5.2.252 SendAsDenied; x not allowed to send "
            "as y (in reply to end of DATA command)": "send_as_denied",
        }
        for reason, cause in cases.items():
            with self.subTest(cause=cause):
                found = _relay_oauth.classify_reason(reason, True, HOST)
                self.assertEqual(found and found[0], cause)

    def test_a_refused_sign_in_reads_by_how_the_relay_signs_in(self):
        oauth = _relay_oauth.classify_reason(AUTH_535, True, "smtp.office365.com")
        self.assertEqual(oauth[0], "token_refused")
        self.assertIn("New-ServicePrincipal", oauth[1])
        password = _relay_oauth.classify_reason(
            AUTH_535, False, "smtp.office365.com",
        )
        self.assertEqual(password[0], "password_refused")
        self.assertIn("Switch the relay to OAuth2", password[1])
        other = _relay_oauth.classify_reason(
            AUTH_535.replace("smtp.office365.com", HOST), False, HOST,
        )
        self.assertNotIn("OAuth2", other[1])

    def test_a_recipient_refused_is_not_the_relay(self):
        self.assertIsNone(_relay_oauth.classify_reason(RCPT, False, HOST))
        self.assertTrue(_relay_oauth.recipient_only(RCPT))

    def test_the_renewer_fails_the_relay_only_when_its_token_runs_out(self):
        now = 1_800_000_000
        error = {"state": "error", "error_code": "AADSTS7000222"}
        lasting = {**error, "expires_at": now + 3600}
        self.assertIsNone(_relay_oauth.token_failure(lasting, now))
        spent = {**error, "expires_at": now + 60}
        cause, message = _relay_oauth.token_failure(spent, now)
        self.assertEqual(cause, "token_AADSTS7000222")
        self.assertIn("expired", message)
        self.assertIsNone(_relay_oauth.token_failure({"state": "ok"}, now))
        unknown = _relay_oauth.token_failure(
            {"state": "error", "error_code": "HTTP 503",
             "error_message": "Service unavailable for a@b.example"}, now,
        )
        self.assertIn("HTTP 503", unknown[1])
        self.assertNotIn("a@b.example", unknown[1])


class _RelayCase(TransactionCase):
    """A deployed production with a password relay."""

    def setUp(self):
        """Create the production; durable writes ride the test cursor."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "rdeliv-host", "ip_address": "192.0.2.82", "user": "ubuntu",
            "wildcard_domain": "rdeliv.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "rdeliv"})
        self.inst = self.env["cloud.instance"].create({
            "name": "rdeliv-prod", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "smtp_relay_host": HOST, "smtp_relay_user": "relay@example.com",
            "smtp_relay_password": "relay-pass",
        })
        self.inst.with_context(cloud_state_transition=True).write(
            {"state": "deployed"},
        )

    def _job(self, code, payload=None):
        """Create a job of *code* on the production."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        return self.env["cloud.job"].create({
            "name": code, "host_id": self.host.id, "instance_id": self.inst.id,
            "job_type_id": job_type.id, "payload": payload or {},
        })

    def _alert(self):
        """Return the active relay alert of the production."""
        return self.env["cloud.alert"].search([
            ("code", "=", _relay_oauth.ALERT_CODE),
            ("instance_id", "=", self.inst.id), ("state", "=", "active"),
        ])

    def _reading(self, reasons=(), age=60, sent_at=None, status=None,
                 lifetime="5d", rc=0):
        """Render what the ``relay_delivery`` step prints."""
        queue = {
            "deferred": 1 if reasons else 0,
            "oldest": [{"age": age, "reasons": list(reasons)}] if reasons else [],
        }
        sent = sent_at.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00") if sent_at else ""
        return (
            f"== status\n{json.dumps(status) if status else ''}\n"
            f"== queue\n{json.dumps(queue)}\n== sent\n{sent}\n"
            f"== lifetime\n{lifetime}\nrc={rc}\n"
        )

    def _probe(self, relay_stdout):
        """Run one probe pass with *relay_stdout* as the relay's reading."""
        executor = InstanceHealthExecutor(self._job("instance_health"), self.host)
        executor._skipped = False
        states = "\n".join(
            f"{svc}\trunning" for svc in self.inst.expected_services()
        )
        results = {
            "container_state": {"stdout": states},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": "exit:0"},
            "error_lines": {"stdout": ""},
            "relay_delivery": {"stdout": relay_stdout},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        return executor

    def _suspicion(self):
        """Return the probe's suspicion of the relay alert, if any."""
        return self.env["cloud.alert.suspicion"].search([
            ("code", "=", _relay_oauth.ALERT_CODE),
            ("instance_id", "=", self.inst.id),
        ])


class TestRelayProbe(_RelayCase):

    def _commands(self, inst=None, state_only=False):
        """Return the probe's step labels for *inst*."""
        job = self._job("instance_health", {"state_only": True} if state_only else {})
        if inst:
            job.instance_id = inst
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        return [c[0] for c in executor.get_commands()]

    def test_the_probe_reads_a_production_s_relay_only(self):
        self.assertIn("relay_delivery", self._commands())
        self.assertNotIn("relay_delivery", self._commands(state_only=True))
        staging = self.env["cloud.instance"].create({
            "name": "rdeliv-stag", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "smtp_relay_host": HOST,
        })
        self.assertNotIn("relay_delivery", self._commands(staging))
        self.inst.smtp_relay_host = False
        self.assertNotIn("relay_delivery", self._commands())

    def test_the_command_keeps_addresses_out(self):
        command = InstanceHealthExecutor(
            self._job("instance_health"), self.host,
        )._relay_delivery_command("~/i", self.inst)
        self.assertIn("postqueue -j", command)
        self.assertIn("relay=smtp.example.com[", command)
        self.assertIn("tail -n 1 | cut -d", command)
        self.assertIn("<address>", command)

    def test_a_failure_alerts_on_the_second_probe_critical_with_its_cause(self):
        self._probe(self._reading([REFUSED]))
        self.assertFalse(self._alert())
        self.assertTrue(self._suspicion())
        self._probe(self._reading([REFUSED]))
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertEqual(alert.payload, {"cause": "connection_refused"})
        self.assertIn("refuses the relay's connection", alert.message)
        self.assertIn("up to 5 days, then goes back to its sender", alert.message)
        self.assertEqual(self.inst.smtp_relay_state, "failing")
        self.assertIn("refuses", self.inst.smtp_relay_error)

    def test_a_recipient_refused_raises_nothing(self):
        for _i in range(3):
            self._probe(self._reading([RCPT], age=7200))
        self.assertFalse(self._alert())

    def test_mail_waiting_too_long_for_an_unknown_reason_is_stuck(self):
        odd = "lost connection with smtp.example.com[192.0.2.5] while sending RCPT TO"
        self._probe(self._reading([odd], age=60))
        self._probe(self._reading([odd], age=60))
        self.assertFalse(self._alert())
        self._probe(self._reading([odd], age=31 * 60))
        self._probe(self._reading([odd], age=36 * 60))
        self.assertEqual(self._alert().payload, {"cause": "stuck"})

    def _raised(self):
        """Raise the alert with two failing probes; return it."""
        self._probe(self._reading([AUTH_535]))
        self._probe(self._reading([AUTH_535]))
        alert = self._alert()
        self.assertTrue(alert)
        return alert

    def test_a_clean_queue_and_a_good_token_do_not_resolve_it(self):
        """Exchange refusing a good token gives both (Fable 1)."""
        self._to_oauth_for_probe()
        alert = self._raised()
        self._probe(self._reading(status={"state": "ok", "expires_at": 9_999_999_999}))
        self.assertEqual(self._alert(), alert)
        self.assertEqual(self.inst.smtp_relay_state, "failing")

    def test_a_delivery_after_the_alert_resolves_it(self):
        alert = self._raised()
        before = alert.create_date - timedelta(minutes=1)
        self._probe(self._reading(sent_at=before))
        self.assertTrue(self._alert())
        after = fields.Datetime.now() + timedelta(seconds=5)
        self._probe(self._reading(sent_at=after))
        self.assertFalse(self._alert())
        self.assertEqual(self.inst.smtp_relay_state, "ok")

    def test_a_relay_that_cannot_be_read_changes_nothing(self):
        alert = self._raised()
        self._probe(self._reading(rc=1))
        self.assertEqual(self._alert(), alert)

    def test_a_relay_that_is_gone_has_its_alert_resolved(self):
        self._raised()
        self.inst.smtp_relay_host = False
        self.assertFalse(self._alert())
        self.assertEqual(self.inst.smtp_relay_state, "unknown")

    def _to_oauth_for_probe(self):
        """Switch the production to OAuth2."""
        self.inst.write({
            "smtp_relay_auth": _relay_oauth.AUTH_OAUTH2_MICROSOFT,
            "smtp_relay_oauth_directory_id": "00000000-0000-4000-8000-0000000000aa",
            "smtp_relay_oauth_client_id": "00000000-0000-4000-8000-0000000000bb",
            "smtp_relay_oauth_client_secret": "s",
        })

    def test_an_oauth2_renewer_out_of_token_names_entra_s_cause(self):
        self._to_oauth_for_probe()
        status = {"state": "error", "error_code": "AADSTS7000215", "expires_at": 0}
        self._probe(self._reading([NO_TOKEN], status=status))
        self._probe(self._reading([NO_TOKEN], status=status))
        alert = self._alert()
        self.assertEqual(alert.payload, {"cause": "token_AADSTS7000215"})
        self.assertIn("client secret", alert.message)


class TestRelayTestMail(_RelayCase):

    def _run(self, answer, exit_status=0):
        """Run the test mail's outcome on *answer*; return the job."""
        job = self._job("relay_test_mail", {"to": "me@example.org", "tag": "t1"})
        executor = RelayTestMailExecutor(job, self.host)
        stdout = f"IC_RELAY_TEST {json.dumps(answer)}\n" if answer else ""
        results = {"Send the relay's test mail": {
            "stdout": stdout, "exit_status": exit_status,
        }}
        errors = executor.parse_results(results)
        if not errors:
            asyncio.run(executor.on_success(results))
        return job, errors

    def test_the_type_waits_alone_and_never_holds_the_queue(self):
        """Fable 4: one waiting at most; a failure stops no backup."""
        job_type = self.env.ref("incubacloud.relay_test_mail")
        self.assertTrue(job_type.queue_exclusive)
        self.assertFalse(job_type.queue_collapse)
        self.assertFalse(job_type.failure_holds_queue)
        self.assertFalse(RelayTestMailExecutor._retry_on_connection_loss)
        job = self._job("relay_test_mail")
        job.state = "failed"
        self.env["cloud.alert"].raise_alert(
            "job_failed", "x", instance=self.inst, job=job,
        )
        self.assertFalse(
            self.env["cloud.job"]._queue_holding_alerts(self.inst.id, self.host.id),
        )

    def test_the_mail_goes_to_the_asker_from_the_relay_s_sender(self):
        job = self._job("relay_test_mail", {"to": "me@example.org", "tag": "t1"})
        executor = RelayTestMailExecutor(job, self.host)
        (label, command, opts), = executor.get_commands()
        self.assertIn("relay_test.sh", command)
        self.assertIn(" relay@example.com me@example.org t1 0 120", command)
        self.assertEqual(opts, {"timeout": 150})

    def test_a_delivered_mail_resolves_the_alert(self):
        self.env["cloud.alert"].raise_alert(
            _relay_oauth.ALERT_CODE, "x", level="critical", instance=self.inst,
            confirm_after=1, confirm_window=1,
        )
        job, errors = self._run({"status": "sent", "response": "250 2.0.0 OK", "token": None})
        self.assertEqual(errors, [])
        self.assertTrue(job.result["delivered"])
        self.assertFalse(self._alert())
        self.assertEqual(self.inst.smtp_relay_state, "ok")

    def test_a_refusal_alerts_at_once_and_the_probe_updates_that_alert(self):
        """Fable 5: one alert, and no suspicion left to confirm another."""
        self.env["cloud.alert.suspicion"]._sight(
            _relay_oauth.ALERT_CODE, None, self.inst, 2, 900,
        )
        job, _errors = self._run({
            "status": "deferred", "response": REFUSED, "token": None,
        })
        self.assertFalse(job.result["delivered"])
        self.assertEqual(job.result["cause"], "connection_refused")
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertFalse(self._suspicion())
        self.assertEqual(self.inst.smtp_relay_state, "failing")
        self._probe(self._reading([REFUSED]))
        self.assertEqual(self._alert(), alert)

    def test_a_timeout_or_a_refused_recipient_is_no_verdict_on_the_relay(self):
        for answer in (
            {"status": "timeout", "response": "", "token": None},
            {"status": "bounced", "response": RCPT, "token": None},
        ):
            with self.subTest(status=answer["status"]):
                job, _errors = self._run(answer)
                self.assertFalse(job.result["delivered"])
                self.assertFalse(self._alert())
                self.assertEqual(self.inst.smtp_relay_state, "unknown")

    def test_only_an_unreachable_relay_fails_the_job(self):
        _job, errors = self._run(None, exit_status=1)
        self.assertTrue(errors)
        _job, errors = self._run(None)
        self.assertEqual(errors, ["The relay's test gave no answer."])


class TestRelayTestMailRequest(_RelayCase):

    def test_it_goes_to_the_asker_and_waits_for_a_deployed_relay(self):
        self.env.user.email = "me@example.org"
        with patch.object(
            type(self.env["cloud.job"]), "enqueue", autospec=True, return_value=7,
        ) as enqueue:
            self.assertEqual(self.inst.send_relay_test_mail(), 7)
        args = enqueue.call_args.args
        self.assertEqual(args[1:4], (self.host.id, self.inst.id, "relay_test_mail"))
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["to"], "me@example.org")
        self.assertRegex(payload["tag"], r"^[0-9a-f]{32}$")

    def test_it_says_why_it_cannot_run(self):
        self.assertEqual(self.inst._relay_test_refusal(), "")
        self.inst.applied_config_hash = "a-hash-of-another-configuration"
        self.assertIn("Apply the changes first", self.inst._relay_test_refusal())
        self.inst.applied_config_hash = False
        self.inst.with_context(cloud_state_transition=True).write({"state": "draft"})
        self.assertIn("Deploy the instance first", self.inst._relay_test_refusal())
        with self.assertRaises(UserError):
            self.inst.send_relay_test_mail()

    def test_an_asker_without_an_address_is_told(self):
        self.env.user.email = False
        with self.assertRaisesRegex(UserError, "no e-mail address"):
            self.inst.send_relay_test_mail()
