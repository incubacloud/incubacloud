"""Send one mail through a production's relay and say whether it left.

The relay's test mail (``cloud.instance.send_relay_test_mail``): one
message from the relay's sender to the person who asked, handed to the
relay on its port 25 and followed in its log to its server's answer
(``scripts/relay_test.sh``). It works for a relay with a password and for
one with OAuth2 alike.

A mail the server did not take is an answer, not a failed job: the job
ends ``done`` with ``delivered`` false and the server's reply. It fails
only when the relay could not be asked (no connection to the host, the
``smtp`` container not answering). Its answer is evidence about the
relay, so it updates the relay's state and alert as the health probe
does, at once: the person who asked has just seen the result.
"""
import json
import logging
import re

from odoo import fields

from . import _relay_oauth
from .abstract_executor import AbstractSSHExecutor

_logger = logging.getLogger(__name__)

_LABEL = "Send the relay's test mail"
_ANSWER_RE = re.compile(r"^IC_RELAY_TEST (\{.*\})\s*$", re.MULTILINE)

#: How long the script waits for the relay's verdict, in seconds. A relay
#: that connects answers in under a second; a server that does not answer
#: costs Postfix's 30 s connection timeout, and docker-mailserver's
#: Amavis adds a few seconds before. Two minutes covers all of it with
#: room, and is what a person waits for at most.
WAIT_SECONDS = 120
#: The step's own timeout: the wait plus the mail's way in and the
#: clean-up after it.
STEP_TIMEOUT = WAIT_SECONDS + 30


class RelayTestMailExecutor(AbstractSSHExecutor):
    """Send the relay's test mail and record what its server answered."""

    _job_type = "relay_test_mail"

    def _inst(self):
        """Return the instance whose relay is tested."""
        return self.job.instance_id

    def get_commands(self):
        """Return the one step that sends the mail and waits for its verdict.

        The sender is the relay's: its mailbox, or the default sender the
        deploy gave Odoo. The recipient and the mail's tag come from the
        payload, set by ``send_relay_test_mail`` from the person who asked.
        """
        inst = self._inst().sudo()
        payload = self.job.payload or {}
        sender = inst.smtp_relay_user or inst.odoo_admin_email or ""
        oauth = "1" if inst._relay_oauth_renders() else "0"
        return [
            (
                _LABEL,
                self.run_script("relay_test.sh", [
                    self._inst_dir(inst),
                    sender,
                    payload.get("to") or "",
                    payload.get("tag") or "",
                    oauth,
                    WAIT_SECONDS,
                ]),
                {"timeout": STEP_TIMEOUT},
            ),
        ]

    def parse_results(self, results):
        """Fail the job only when the relay could not be asked.

        :param dict results: the steps' results
        :returns: error strings; none when the script printed its answer
        :rtype: list
        """
        step = results.get(_LABEL) or {}
        if step.get("exit_status") != 0:
            return [f"'{_LABEL}' exited with status {step.get('exit_status')}"]
        if self._parse_answer(step.get("stdout")) is None:
            return ["The relay's test gave no answer."]
        return []

    @staticmethod
    def _parse_answer(stdout):
        """Return the script's answer as a dict, or None.

        :param str stdout: the step's output
        :rtype: dict or None
        """
        matches = _ANSWER_RE.findall(stdout or "")
        if not matches:
            return None
        try:
            answer = json.loads(matches[-1])
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None

    def _verdict(self, answer, inst):
        """Turn the script's answer into what the panel says of the relay.

        :param dict answer: ``status``, ``response`` and ``token``
        :param inst: the ``cloud.instance``
        :returns: ``(delivered, cause, message)``; *cause* is None when
            the answer says nothing about the relay as a whole (a timeout,
            or the server refusing the one recipient)
        :rtype: tuple
        """
        status = answer.get("status")
        response = _relay_oauth.without_addresses(answer.get("response") or "")
        if status == "sent":
            return True, None, ""
        oauth = inst._relay_oauth_renders()
        token = answer.get("token") if oauth else None
        failure = _relay_oauth.token_failure(
            token if isinstance(token, dict) else None,
            fields.Datetime.now().timestamp(),
        ) or _relay_oauth.classify_reason(response, oauth, inst.smtp_relay_host)
        if failure:
            return False, failure[0], failure[1]
        if status == "timeout":
            return False, None, (
                f"The relay did not hand the mail over within "
                f"{WAIT_SECONDS // 60} minutes. Its server may be slow; "
                f"look at the relay's log."
            )
        if _relay_oauth.recipient_only(response):
            return False, None, (
                f"The server refused your address as a recipient: {response}"
            )
        # A refusal none of the causes names, at the end of the mail (a
        # bounce): still the relay's server refusing what the relay sends.
        return False, "refused", (
            f"{inst.smtp_relay_host} refused the mail: {response}"
        )

    async def on_success(self, results):
        """Record the verdict on the job, the relay's state and its alert.

        Delivered: the relay works, so its alert is resolved. Refused for
        a cause of the relay's: the alert is raised at once, without the
        two sightings the probe waits for, and the probe's suspicion of it
        is dropped, so the probe updates this alert instead of confirming
        a second one ten minutes later. Anything else (a timeout, the
        recipient refused) leaves the state and the alert as they were.
        """
        inst = self._inst()
        answer = self._parse_answer(results.get(_LABEL, {}).get("stdout"))
        delivered, cause, message = self._verdict(answer, inst)
        response = _relay_oauth.without_addresses(answer.get("response") or "")
        self._write_own_job({"result": {
            "delivered": delivered,
            "status": answer.get("status"),
            "response": response[:500],
            "cause": cause,
            "message": message,
        }})
        now = fields.Datetime.now()
        if delivered:
            inst.sudo().write({
                "smtp_relay_state": "ok",
                "smtp_relay_checked_at": now,
                "smtp_relay_error": False,
            })
            self._sys(f"✓ Delivered: {response}")
            with self._durable_env() as env:
                env["cloud.alert"].resolve_alert(
                    _relay_oauth.ALERT_CODE,
                    instance=env["cloud.instance"].browse(inst.id),
                )
            return
        self._sys(f"✗ Not delivered ({answer.get('status')}): {message}")
        if not cause:
            return
        inst.sudo().write({
            "smtp_relay_state": "failing",
            "smtp_relay_checked_at": now,
            "smtp_relay_error": message[:500],
        })
        with self._durable_env() as env:
            instance = env["cloud.instance"].browse(inst.id)
            env["cloud.alert"].raise_alert(
                _relay_oauth.ALERT_CODE, message, level="critical",
                instance=instance,
                job=env["cloud.job"].browse(self.job.id),
                payload={"cause": cause},
                confirm_after=1, confirm_window=1,
            )
            env["cloud.alert.suspicion"]._forget(
                _relay_oauth.ALERT_CODE, None, instance,
            )
