"""Delete an archived instance's backup chain, then its record.

An archived instance is a record whose only remaining substance is the
chain in the bucket. Deleting it therefore cannot be a plain ``unlink``:
the invariant this whole feature rests on is that objects never outlive
the instance they belong to, and an unlink would leave the chain behind
with nothing left that even knows where it is.

There is no stack to purge from — archiving tore it down — so the work
runs in a container created for this one command and thrown away. See
``scripts/backup_purge_archived.sh`` for why that beats reviving the
instance in order to delete it.

The record is unlinked in ``on_success`` and nowhere else. A purge that
fails leaves the archived instance exactly as it was, which is the only
outcome that keeps the invariant true: better a record that refuses to
go than a bucket nobody can reach any more.
"""
import logging

from odoo import _
from odoo.exceptions import UserError

from . import sftp_dst
from ._backup_dst import env_line
from .abstract_executor import AbstractSSHExecutor, JobRefused

_logger = logging.getLogger(__name__)

#: Label of the single step, looked up by the failure classifier.
PURGE_ARCHIVED_LABEL = "Purge archived backups"

#: ``scripts/backup_purge_archived.sh`` exit codes mapped to the alert
#: each one raises, same contract as the delete executor's: the script
#: classifies from its own vantage point and this side never parses text.
PURGE_ARCHIVED_ALERT_BY_EXIT = {
    20: (
        "archived_purge_env_missing",
        "The credentials for %(name)s's archived copy never reached the "
        "host, so nothing could be deleted. Check that its backup "
        "destination still exists in the panel.",
    ),
    21: (
        "archived_purge_unauthorized",
        "The storage rejected the credentials while deleting %(name)s's "
        "archived copy. Check or regenerate them on the backup "
        "destination, then delete the instance again.",
    ),
    22: (
        "archived_purge_failed",
        "Could not delete %(name)s's archived copy. See the job log; the "
        "purge image failing to pull or to start is the usual cause.",
    ),
    23: (
        "archived_purge_chain_continued",
        "%(name)s's archived copy was not deleted: the instance that took "
        "its name continued its backup chain, and deleting the copy would "
        "leave that instance's later backups without the set they build "
        "on. Nothing was deleted. Once that instance has a full backup of "
        "its own, remove its incremental sets older than that full and "
        "delete again (see the triage runbook).",
    ),
}

#: The alert of a purge stopped before its step, under the code of 22.
PURGE_ARCHIVED_REFUSED = (
    "Could not delete %(name)s's archived copy: the purge stopped before "
    "it started. %(why)s"
)

#: The prefix was already empty — the chain is gone by other means. Not
#: a failure: the invariant already holds, so the record may go.
PURGE_ARCHIVED_EXIT_ALREADY_EMPTY = 10

#: The SFTP key and trusted server key, beside the first environment file
#: (``backup_purge_archived.sh`` looks for them there).
SFTP_KEY_NAME = "purge-ssh-key"
SFTP_KNOWN_HOSTS_NAME = "purge-known_hosts"


class PurgeArchivedBackupsExecutor(AbstractSSHExecutor):
    """Empty an archived instance's prefix, then remove its record."""

    _job_type = "purge_archived_backups"
    # Its instance is archived by definition: the queue must not cancel
    # it for that (``AbstractExecutor._runs_on_archived_instance``).
    _runs_on_archived_instance = True

    def _inst(self):
        # ``active_test=False``: the whole point is that this instance is
        # archived, so every default read would come back empty.
        return self.job.with_context(active_test=False).instance_id

    def _env_path(self):
        """Remote path of the uploaded environment file, in the job's directory."""
        return self._job_path("purge.env")

    def _extra_prefixes(self):
        """The other prefixes of the copy, emptied with the chain.

        What the instance answers (``_archived_copy_extra_prefixes``):
        nothing in core, where the copy is the duplicity chain alone; an
        addon that keeps part of the copy beside it names its prefix.

        :return: one DST per further prefix, in the order they are emptied
        :rtype: list
        """
        return list(self._inst()._archived_copy_extra_prefixes())

    def _dsts(self):
        """Every prefix the job empties, the chain's first when it has one.

        An archived instance with no chain path (none was frozen, or it
        was cleared by hand) may still have a further prefix, and
        ``delete_archived`` sends it here for that one: it is then the
        first.

        :return: one DST per environment file, in ``_env_paths``'s order
        :rtype: list
        """
        inst = self._inst()
        return [
            dst for dst in (inst.custom_backup_dst, *self._extra_prefixes())
            if dst
        ]

    def _env_paths(self):
        """Remote paths of every environment file, the chain's first.

        :return: ``purge.env``, then ``purge-<n>.env`` per further prefix
        :rtype: list
        """
        return [self._env_path()] + [
            self._job_path(f"purge-{n}.env")
            for n in range(1, len(self._dsts()))
        ]

    def _env_contents(self):
        """Render one environment per prefix, in ``_env_paths``'s order.

        :return: ``{remote path: content}``
        :rtype: dict
        """
        return {
            path: self._env_content(dst)
            for path, dst in zip(self._env_paths(), self._dsts())
        }

    def _env_content(self, dst=None):
        """Render the environment the ephemeral container needs.

        Deliberately the same variable names the ``backup`` service was
        deployed with: the destination is read from ``DST`` and the
        credentials from the standard AWS variables, so the purge sees
        exactly what duplicity saw when it wrote the chain.

        ``custom_backup_dst`` and not the computed path — the frozen
        value is the only one still true once the project can have moved
        underneath the record.

        ``PURGE_BEFORE`` carries the instant the deletion was decided,
        so the purge is bounded to the chain that existed then. Without
        it a job that lands late — a retry, a queue that drained slowly —
        would empty whatever holds the prefix by the time it runs, and
        after "start from scratch" that is the customer's new instance.

        Nor what an instance of the same name that took this one's path
        stored there: the cutoff is that instance's creation when it is
        earlier (``cloud.instance._backup_purge_before``), so a delete
        decided after its first copy, or of an archive it outlived,
        leaves its copies. Should it have continued this chain, the
        program deletes nothing and exits 23.

        Nor, when this one took the name of an instance created before it
        that holds the same path, archived as well, what that one stored
        there: ``PURGE_SINCE`` is this one's creation
        (``cloud.instance._backup_purge_since``), and the purge keeps
        what is older for the other's own.

        A further prefix of the copy (*dst*) gets the same environment
        with its own ``DST``: the same destination's credentials, and the
        same bounds, for the same reason.

        :param str dst: a further prefix (``_extra_prefixes``), or None
            for the chain's frozen one
        :rtype: str
        :raises ValueError: when the destination would not stay on its
            line (``env_line``): the job fails before the host is reached
        """
        inst = self._inst()
        backend = inst.effective_backup_backend.sudo()
        dst = dst or inst.custom_backup_dst
        lines = [env_line("DST", dst)]
        if inst.purge_cutoff_at:
            before = inst._backup_purge_before(dst, inst.purge_cutoff_at)
            lines.append(env_line("PURGE_BEFORE", before.isoformat()))
        since = inst._backup_purge_since(dst)
        if since:
            lines.append(env_line("PURGE_SINCE", since))
        if backend.backend_type == "sftp":
            # The key and the trusted server key travel as files of their
            # own (``_sftp_files``), mounted where the program reads them;
            # a password, as in ``backup.env``, as a line of the file.
            if backend.sftp_auth_method == "password":
                lines.append(
                    env_line("BACKEND_PASSWORD", backend.sftp_password or "")
                )
            return "\n".join(lines) + "\n"
        if backend.s3_access_key_id:
            lines.append(env_line("AWS_ACCESS_KEY_ID", backend.s3_access_key_id))
        if backend.s3_secret_access_key:
            lines.append(
                env_line("AWS_SECRET_ACCESS_KEY", backend.s3_secret_access_key)
            )
        if backend.s3_endpoint_url:
            lines.append(env_line("AWS_ENDPOINT_URL", backend.s3_endpoint_url))
        if backend.s3_region:
            lines.append(env_line("AWS_DEFAULT_REGION", backend.s3_region))
        return "\n".join(lines) + "\n"

    async def before_execute(self, transport):
        """Put the environment in the job's own directory, owner-only.

        Uploaded rather than passed as arguments: an argument is visible
        in ``ps`` to every account on the host for as long as the
        container runs, and this one carries the destination's
        credentials. Created 0600 in the job's directory (0700, named at
        random, created by this job): no moment when another account
        could read it, and no directory of another account to land in —
        the per-job name this used, ``install -d`` unchecked, was one
        anybody could create first. The script removes the files on its
        way out, and the job its directory, failure included. One file
        per prefix of the copy (``_env_contents``).

        Refused before anything is uploaded when a prefix would reach more
        than this instance's copy (``_refuse_shared_prefix``).
        """
        self._refuse_shared_prefix()
        await self._upload_private_files(
            transport, {**self._env_contents(), **self._sftp_files()},
        )

    def _sftp_files(self):
        """Return the SFTP key and trusted server key, for an SFTP copy.

        Written beside the environment files, 0600 in the job's directory,
        and mounted read-only by ``backup_purge_archived.sh``, which
        removes them on its way out. Every prefix is checked first: a
        path off the destination's server, or not one instance's folder,
        stops the job before the key leaves the panel. A destination that
        logs in with a password sends the server key only: the password
        is in the environment files (``_env_content``).

        :return: ``{remote path: content}``, empty for an S3 destination
        :rtype: dict
        :raise UserError: when a prefix is not this destination's, or the
            server key was never trusted
        """
        backend = self._inst().effective_backup_backend.sudo()
        if backend.backend_type != "sftp":
            return {}
        for dst in self._dsts():
            try:
                path = sftp_dst.check_dst(
                    backend.sftp_user, backend.sftp_host, backend.sftp_port, dst,
                )
            except sftp_dst.DstError as exc:
                raise UserError(_(
                    "%(path)s is not on the server of the backup destination "
                    "%(name)s: nothing was deleted.",
                    path=dst, name=backend.name,
                )) from exc
            if not sftp_dst.instance_path(path):
                raise UserError(_(
                    "%(path)s is not one instance's folder: nothing was deleted.",
                    path=dst,
                ))
        password_login = backend.sftp_auth_method == "password"
        if not backend.sftp_known_hosts or not (
            password_login or backend.sftp_private_key
        ):
            raise UserError(_(
                "Confirm the SFTP server's key on the backup destination "
                "%(name)s: nothing was deleted.",
                name=backend.name,
            ))
        if password_login and not backend.sftp_password:
            raise UserError(_(
                "Enter the SFTP password again on the backup destination "
                "%(name)s: nothing was deleted.",
                name=backend.name,
            ))
        files = {
            self._job_path(SFTP_KNOWN_HOSTS_NAME): backend.sftp_known_hosts.strip() + "\n",
        }
        if not password_login:
            key = backend.sftp_private_key
            files[self._job_path(SFTP_KEY_NAME)] = (
                key if key.endswith("\n") else key + "\n"
            )
        return files

    def _refuse_shared_prefix(self):
        """Refuse a prefix whose purge would reach another's copies.

        A destination's own path, or one that is or holds another
        instance's (``cloud.instance._backup_prefix_conflict``): fails
        closed, the record stays and ``on_abort`` raises the alert.

        :raises JobRefused: naming the prefix and what it would reach
        """
        inst = self._inst()
        for dst in self._dsts():
            refusal = inst._backup_prefix_refusal(dst)
            if refusal:
                raise JobRefused(refusal)

    def get_commands(self):
        image = (
            self.env["cloud.settings"].sudo()._get_system().archived_purge_image
        )
        return [(
            PURGE_ARCHIVED_LABEL,
            self.run_script(
                "backup_purge_archived.sh",
                [self._env_path(), image, *self._env_paths()[1:]],
            ),
            {"stop_on_failure": True},
        )]

    def parse_results(self, results):
        """Treat "already empty" as success, everything else as reported.

        Exit 10 means the prefix holds nothing. The record is free to go:
        what the deletion had to guarantee is already true.
        """
        step = results.get(PURGE_ARCHIVED_LABEL) or {}
        if step.get("exit_status") == PURGE_ARCHIVED_EXIT_ALREADY_EMPTY:
            return []
        return super().parse_results(results)

    async def on_success(self, results):
        """Drop the record now that nothing of it is left in the bucket.

        The order is the point: the objects go first and the record only
        afterwards, so an interruption anywhere in between leaves a
        record whose chain is empty — recoverable by deleting it again —
        rather than a chain with no record, which nothing could ever
        find.
        """
        inst = self._inst()
        name = inst.name if inst else "?"
        self._sys(f"✓ Archived copy of '{name}' deleted.")
        if not inst:
            return
        Alert = self.env["cloud.alert"].sudo()
        # With the host they were raised with: an alert's identity
        # includes it.
        for code, _msg in PURGE_ARCHIVED_ALERT_BY_EXIT.values():
            Alert.resolve_alert(code, host=self._host(), instance=inst)
        Alert.resolve_alert("archive_copy_lost", instance=inst)
        # Deferred, not done here: an unlink from the hook cascades onto
        # this job's row and costs the job its commit (see
        # ``_unlink_after_job_commit``). The record is archived, so a
        # re-run after a dropped unlink simply purges an empty prefix
        # and arms it again.
        self._unlink_after_job_commit(inst, sudo=True)
        _logger.info(
            "archived instance %s: chain purged; record goes once the "
            "job commits", name,
        )
        # Whatever the instance leaves behind — an emptied project, a
        # layer's link — is cleaned up by ``unlink`` itself, layer by
        # layer. Doing it from here would mean core deciding to delete a
        # project because its last instance went, which may be true of a
        # layer's disposable project and is wrong for an operator's.
        await self._after_purge()

    async def _after_purge(self):
        """Hook: the copy is gone and the record with it.

        Empty in core, where deleting an archived instance is the end of
        the story. A module that owns a lifecycle *above* the instance —
        a customer account, say — may need to act exactly here and nowhere
        earlier. Not to keep a fresh instance of the same name from this
        purge: that one gets a backup path of its own
        (``cloud.instance._claim_own_backup_path``), and one that shared
        the path is protected by the cutoff
        (``cloud.instance._backup_purge_before``).

        Called after the unlink, so an override that needs anything off
        the instance has to read it before, in its own ``on_success``.
        """

    async def on_abort(self, exc):
        """A purge stopped before its step fails as one that ran did.

        Its reason in the job's log, the alert and the record kept: a
        prefix refused (``_refuse_shared_prefix``), a value that would
        break its line in the environment (``env_line``).

        :param Exception exc: what stopped the run
        """
        await self.on_failure({}, [str(exc)])

    async def on_failure(self, results, errors):
        """Report, alert, and leave the record standing."""
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if not inst:
            return
        exit_status = (
            results.get(PURGE_ARCHIVED_LABEL) or {}
        ).get("exit_status")
        code_message = PURGE_ARCHIVED_ALERT_BY_EXIT.get(exit_status)
        if not code_message:
            code_message = PURGE_ARCHIVED_ALERT_BY_EXIT[22]
        code, template = code_message
        message = template % {"name": inst.name}
        if not results:
            # Stopped before the step (``on_abort``): the reason is the
            # message, not the image.
            message = PURGE_ARCHIVED_REFUSED % {
                "name": inst.name, "why": "; ".join(errors),
            }
        self.env["cloud.alert"].sudo().raise_alert(
            code,
            message,
            level="warning",
            host=self._host(),
            instance=inst,
            job=self.job,
        )
        self._sys(
            "The instance was NOT deleted: its archived copy is still in "
            "the storage, and removing the record now would leave it "
            "there with nothing able to find it. Fix the cause above and "
            "delete again."
        )
