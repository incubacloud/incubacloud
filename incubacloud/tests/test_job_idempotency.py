"""Core's job types, run once, twice and cut off (``idempotency_harness``).

The specifications here are core's; a repository layered on top may run
the same harness over its own types. Every type core declares has a
specification or an exemption that says why, and the coverage test
fails on one that has neither: a type added later is either specified
or exempted on purpose.

Specified here: ``delete_instance`` and ``restore_instance``,
``relay_test_mail``, which came with the relay's test mail, and
``check_admin_password``, which came with the check of the admin's default
password. The rest wait.
"""
import contextlib
import dataclasses
import json
import shlex
import time
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.abstract_executor import HANDOFF_PREFIX, handoff_archive_path
from ..models.transport import CommandResult
from .idempotency_harness import (
    Call,
    IdempotencyCase,
    JobSpec,
    RepeatsOnceCalls,
    uncovered_job_types,
)

#: The core repository: the types its addons declare are checked here.
CORE_ROOT = Path(__file__).resolve().parents[2]

_PENDING = "pending: not specified yet"

#: Core's types without a specification yet, and why.
EXEMPT = {
    code: _PENDING for code in (
        "backup_create", "backup_download", "backup_download_neutralized",
        "backup_list", "backup_restore", "capture_secret_key", "delete_host",
        "deploy_instance", "deploy_metrics_central", "discard_handoff",
        "docker_prune", "export_instance", "full_setup",
        "grant_restore_upload_key", "host_hardening", "host_metrics",
        "host_probe", "host_reboot", "host_reboot_check",
        "install_observability", "instance_health",
        "move_cleanup_source", "move_cutover", "move_fingerprint",
        "move_freeze_source", "move_quiet_check", "move_rollback_cleanup",
        "pitr_base_backup", "pitr_disable", "pitr_enable", "pitr_purge",
        "pitr_restore", "pitr_status", "purge_archived_backups",
        "push_edge_guard", "push_trusted_proxies", "read_last_login",
        "rebuild_instance",
        "restart_instance", "revoke_restore_upload_key", "setup_whitelist",
        "start_instance", "stop_instance", "sync_direct_path",
        "sync_metrics_accounts", "verify_restore_upload",
        "watch_restore_uploads",
    )
}


def _host(case, name, ip):
    """A host the harness's transport answers for."""
    return case.env["cloud.host"].create({
        "name": name,
        "ip_address": ip,
        "user": "ubuntu",
        "wildcard_domain": f"{name}.example.com",
    })


def _deployed_instance(case, host, name):
    """An instance deployed on *host*, in a project of its own."""
    project = case.env["cloud.project"].create({"name": f"Idempotency {name}"})
    inst = case.env["cloud.instance"].create({
        "name": name,
        "project_id": project.id,
        "environment": "production",
        "host_id": host.id,
    })
    inst._transition("deploying")
    inst._transition("deployed")
    return inst


def _delete_setup(case):
    """A deployed instance and its dispatched delete."""
    host = _host(case, "idem-delete", "192.0.2.81")
    inst = _deployed_instance(case, host, "idem-delete-inst")
    return case.dispatched_job("delete_instance", host, inst)


DELETE_SPEC = JobSpec(code="delete_instance", setup=_delete_setup)


def _restore_setup(case):
    """A cross-host ``from_host`` restore, the step of a move of 7-nov.

    The archive waits in its handoff directory on the source host; the
    restore runs on the target.
    """
    target = _host(case, "idem-restore-target", "192.0.2.82")
    source = _host(case, "idem-restore-source", "192.0.2.83")
    inst = _deployed_instance(case, target, "idem-restore-inst")
    download_type = case.env["cloud.job.type"].search(
        [("code", "=", "backup_download")], limit=1,
    )
    download = case.env["cloud.job"].sudo().create({
        "host_id": source.id,
        "instance_id": inst.id,
        "job_type_id": download_type.id,
        "name": "Download",
    })
    return case.dispatched_job("restore_instance", target, inst, payload={
        "mode": "from_host", "source_job_id": download.id,
    })


def _restore_remote(case, job):
    """The archive handed over on the source host."""
    download = case.env["cloud.job"].browse(job.payload["source_job_id"])
    return {(download.host_id.id, handoff_archive_path(case.env, download.id))}


def _on_a_host(ledger, host, method, detail):
    """Answer ``test -f`` from the remote state; ``rm -rf`` changes it."""
    if detail.startswith("test -f "):
        path = detail[len("test -f "):]
        present = (host.id, path) in ledger.remote
        return CommandResult(stdout="", exit_status=0 if present else 1)
    if detail.startswith("rm -rf "):
        gone = detail[len("rm -rf "):]
        ledger.remote -= {
            (where, path) for where, path in ledger.remote
            if where == host.id and path.startswith(gone)
        }
    return None


async def _fetch_the_handoff(executor, _transport):
    """What the real preparation needs first: the archive on the source.

    Stands in for ``before_execute``, which streams the archive from the
    source host to this one through the panel's disk: it fails as it
    does when the archive is gone (7-nov-2026, "No such file").
    """
    source = executor._source_job()
    async with source.host_id.get_transport() as src:
        found = await src.run(
            f"test -f {handoff_archive_path(executor.env, source.id)}",
        )
    if found.exit_status:
        raise RuntimeError("No such file: the handed-over archive is gone.")


def _removes_the_handoff(call):
    """The removal of the handoff on the source host must happen once."""
    return call.kind == "ssh" and call.detail.startswith(f"rm -rf {HANDOFF_PREFIX}")


_DIES_AFTER_THE_REMOVAL = (
    "pending C3: the restore cannot resume once its on_success removed"
    " the handoff and died before committing"
)

RESTORE_SPEC = JobSpec(
    code="restore_instance",
    setup=_restore_setup,
    remote=_restore_remote,
    answer=_on_a_host,
    before=_fetch_the_handoff,
    commands=[("Restore database", "true"), ("Start Odoo service", "true")],
    once=_removes_the_handoff,
    # The handoff on the source is removed inside on_success: a worker
    # that dies after that, before the hook's cursor commits, leaves the
    # next attempt without its archive. Delivery C3 makes the restore
    # resumable.
    divergent={"hook": _DIES_AFTER_THE_REMOVAL},
    divergent_after=((_removes_the_handoff, _DIES_AFTER_THE_REMOVAL),),
)


def _relay_test_setup(case):
    """A production whose relay signs in with a password, and its test.

    The job is created as the test's user, whose address the mail goes
    to (``RelayTestMailExecutor.get_commands``).
    """
    host = _host(case, "idem-relay", "192.0.2.84")
    inst = _deployed_instance(case, host, "idem-relay-inst")
    inst.write({
        "smtp_relay_host": "smtp.example.com",
        "smtp_relay_user": "relay@example.com",
        "smtp_relay_password": "relay-pass",
    })
    case.env.user.email = "asker@example.com"
    return case.dispatched_job("relay_test_mail", host, inst)


def _sends_the_test_mail(call):
    """The host call that hands the mail to the relay."""
    return (
        call.kind == "ssh" and call.method in ("execute", "run")
        and "relay_test.sh" in call.detail
    )


def _is_a_mail(call):
    """A mail the relay took: it must happen once.

    Recorded apart from the host call that sends it, whose command
    carries a tag and a staging directory new on every attempt.
    """
    return call.kind == "mail"


def _the_relay_takes_it(ledger, host, method, detail):
    """The script's answer: the relay's server took the mail."""
    if method in ("execute", "run") and "relay_test.sh" in detail:
        recipient = shlex.split(detail)[4]
        ledger.calls.append(Call("mail", host.id, "relay_test", recipient))
        answer = {"status": "sent", "response": "250 2.0.0 Ok", "token": None}
        return CommandResult(
            stdout=f"IC_RELAY_TEST {json.dumps(answer)}\n", exit_status=0,
        )
    return None


_SENDS_IT_AGAIN = RepeatsOnceCalls(
    "the mail left before anything stores it: an attempt that dies after"
    " the relay took it and before on_success commits sends the person a"
    " second test mail, which says the same of the relay"
)

#: The relay's test mail. No retry on a lost connection
#: (``_retry_on_connection_loss``), no point of no return (a stop removes
#: the waiting mail from the relay's queue, ``relay_test.sh``), and a
#: failure holds no queue. What it may not repeat is the mail itself; the
#: two scenarios that do repeat it must still leave the reference world.
RELAY_TEST_SPEC = JobSpec(
    code="relay_test_mail",
    setup=_relay_test_setup,
    answer=_the_relay_takes_it,
    once=_is_a_mail,
    divergent={"hook": _SENDS_IT_AGAIN},
    divergent_after=((_sends_the_test_mail, _SENDS_IT_AGAIN),),
)


def _admin_check_setup(case):
    """A running production still to be checked, and its dispatched check."""
    host = _host(case, "idem-admin", "192.0.2.85")
    inst = _deployed_instance(case, host, "idem-admin-inst")
    inst.sudo().write({"running": True})
    return case.dispatched_job("check_admin_password", host, inst)


def _admin_check_remote(_case, job):
    """The instance's database, its admin still on Odoo's default."""
    return {(job.host_id.id, "admin:default")}


def _the_database_answers(ledger, host, method, detail):
    """``admin_password.sh`` against the database the ledger pictures.

    On the default it changes the password, marks the change with the
    second it was made (``admin:mark:<epoch>``) and answers ``changed``;
    on a password of its own it answers ``changed`` again only when the
    mark is newer than the ``since`` it was given, as the script does.
    """
    if method not in ("execute", "run") or "admin_password.sh" not in detail:
        return None
    since = int(shlex.split(detail)[-1])
    if (host.id, "admin:default") in ledger.remote:
        ledger.remote.discard((host.id, "admin:default"))
        ledger.remote.add((host.id, f"admin:mark:{int(time.time())}"))
        answer = "changed"
    else:
        marks = [
            int(path.rsplit(":", 1)[1]) for where, path in ledger.remote
            if where == host.id and path.startswith("admin:mark:")
        ]
        answer = "changed" if marks and max(marks) > since else "own"
    return CommandResult(stdout=f"ADMIN_PASSWORD {answer}\n", exit_status=0)


#: The check of a running instance's admin password. Its one step changes
#: the password and, in the same transaction, marks the change in the
#: instance's database: an attempt cut after that step, its answer lost,
#: is followed by one that finds the mark newer than the instance's last
#: answer and answers ``changed`` again, so the alert and the state are
#: the reference's. One that finds the state kept by an attempt before it
#: is skipped.
ADMIN_CHECK_SPEC = JobSpec(
    code="check_admin_password",
    setup=_admin_check_setup,
    remote=_admin_check_remote,
    answer=_the_database_answers,
)


class TestCoreJobTypesRunTwice(IdempotencyCase):
    """The specified core types leave one attempt's world, whatever happens."""

    def test_delete_instance(self):
        """Removed once: the unlink owed after a lost commit is armed again."""
        self.check_spec(DELETE_SPEC)

    def test_restore_instance(self):
        """A restore whose commit is lost does not look for its archive again."""
        self.check_spec(RESTORE_SPEC)

    def test_relay_test_mail(self):
        """A test whose commit is lost does not send its mail again."""
        self.check_spec(RELAY_TEST_SPEC)

    def test_check_admin_password(self):
        """A change whose answer is lost is told by the next attempt."""
        self.check_spec(ADMIN_CHECK_SPEC)


def _the_relay_takes_it_and_the_host_changes(ledger, host, method, detail):
    """As ``_the_relay_takes_it``; a second mail also changes the host's row.

    A world that diverges in the very scenarios known to repeat the mail.
    """
    answer = _the_relay_takes_it(ledger, host, method, detail)
    if len(ledger.of("mail")) > 1:
        host.env.cr.execute(
            'UPDATE cloud_host SET "user" = %s WHERE id = %s',
            ("changed", host.id),
        )
    return answer


class TestARepeatedOnceCallKeepsTheWorld(IdempotencyCase):
    """A scenario known to repeat a once-only call is still held to the world."""

    def test_a_world_behind_the_repeated_mail_fails(self):
        """The test mail sent twice may not hide a change to the database."""
        spec = dataclasses.replace(
            RELAY_TEST_SPEC, answer=_the_relay_takes_it_and_the_host_changes,
        )
        # Raise the subtest's failure here instead of recording it.
        with patch.object(
            type(self), "subTest",
            lambda _self, **_kw: contextlib.nullcontext(),
        ), self.assertRaisesRegex(AssertionError, "left another world"):
            self.check_spec(spec)


def _the_database_forgets_its_mark(ledger, host, method, detail):
    """As ``_the_database_answers``, with no mark: ``own`` after a change."""
    answer = _the_database_answers(ledger, host, method, detail)
    if answer and (host.id, "admin:default") not in ledger.remote:
        seen = (host.id, "admin:answered")
        if seen in ledger.remote:
            return CommandResult(stdout="ADMIN_PASSWORD own\n", exit_status=0)
        ledger.remote.add(seen)
    return answer


class TestALostAnswerNeedsTheMark(IdempotencyCase):
    """Without the mark, a check cut after its change loses the change."""

    def test_a_check_that_forgets_its_change_leaves_another_world(self):
        """The cut after the step: the next attempt says ``own``, and the
        alert the reference raised is never raised."""
        spec = dataclasses.replace(
            ADMIN_CHECK_SPEC, answer=_the_database_forgets_its_mark,
        )
        with patch.object(
            type(self), "subTest",
            lambda _self, **_kw: contextlib.nullcontext(),
        ), self.assertRaisesRegex(AssertionError, "another world"):
            self.check_spec(spec)


class TestEveryCoreJobTypeIsSpecified(TransactionCase):
    """A core job type has a specification or an exemption with its reason."""

    def test_no_type_is_left_out(self):
        """The registry's core types are all accounted for."""
        specs = {
            DELETE_SPEC.code, RESTORE_SPEC.code, RELAY_TEST_SPEC.code,
            ADMIN_CHECK_SPEC.code,
        }
        missing, unreasoned = uncovered_job_types(
            self.env, CORE_ROOT, specs, EXEMPT,
        )
        self.assertEqual(missing, [], "core job types with neither a"
                         " specification nor an exemption")
        self.assertEqual(unreasoned, [], "exemptions without a reason")
        self.assertFalse(specs & set(EXEMPT), "specified and exempted at once")
