"""Core's job types, run once, twice and cut off (``idempotency_harness``).

The specifications here are core's; the saas repository runs the same
harness over its own types. Every type core declares has a
specification or an exemption that says why, and the coverage test
fails on one that has neither: a type added later is either specified
or exempted on purpose.

Delivery C1 of the idempotency plan (8-nov-2026) specifies three types:
``delete_instance``, ``restore_instance`` here and
``warm_rebuild_instance`` in saas. The rest wait for C2-C5.
"""
from pathlib import Path

from odoo.tests.common import TransactionCase

from ..models.abstract_executor import HANDOFF_PREFIX, handoff_archive_path
from ..models.transport import CommandResult
from .idempotency_harness import IdempotencyCase, JobSpec, uncovered_job_types

#: The core repository: the types its addons declare are checked here.
CORE_ROOT = Path(__file__).resolve().parents[2]

_PENDING = "pending: the idempotency plan's deliveries C2-C5"

#: Core's types without a specification yet, and why.
EXEMPT = {
    code: _PENDING for code in (
        "backup_create", "backup_download", "backup_download_neutralized",
        "backup_list", "backup_restore", "capture_secret_key", "delete_host",
        "deploy_instance", "deploy_metrics_central", "discard_handoff",
        "docker_prune", "export_instance", "full_setup",
        "grant_restore_upload_key", "host_hardening", "host_metrics",
        "host_probe", "install_observability", "instance_health",
        "move_cleanup_source", "move_cutover", "move_fingerprint",
        "move_freeze_source", "move_quiet_check", "move_rollback_cleanup",
        "pitr_base_backup", "pitr_disable", "pitr_enable", "pitr_restore",
        "pitr_status", "purge_archived_backups", "push_edge_guard",
        "push_trusted_proxies", "read_last_login", "rebuild_instance",
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


class TestCoreJobTypesRunTwice(IdempotencyCase):
    """The specified core types leave one attempt's world, whatever happens."""

    def test_delete_instance(self):
        """Removed once: the unlink owed after a lost commit is armed again."""
        self.check_spec(DELETE_SPEC)

    def test_restore_instance(self):
        """A restore whose commit is lost does not look for its archive again."""
        self.check_spec(RESTORE_SPEC)


class TestEveryCoreJobTypeIsSpecified(TransactionCase):
    """A core job type has a specification or an exemption with its reason."""

    def test_no_type_is_left_out(self):
        """The registry's core types are all accounted for."""
        specs = {DELETE_SPEC.code, RESTORE_SPEC.code}
        missing, unreasoned = uncovered_job_types(
            self.env, CORE_ROOT, specs, EXEMPT,
        )
        self.assertEqual(missing, [], "core job types with neither a"
                         " specification nor an exemption")
        self.assertEqual(unreasoned, [], "exemptions without a reason")
        self.assertFalse(specs & set(EXEMPT), "specified and exempted at once")
