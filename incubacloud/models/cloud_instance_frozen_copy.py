"""The frozen copy a move leaves on the old host, for ``cloud.instance``.

Extracted verbatim from ``cloud_instance.py``, like
``cloud_instance_move.py``: ``_inherit`` keeps everything on the same
model, same table, same fields, same xml_ids for the crons, so nothing
here changed behaviour when it moved.

What lives here: whether the copy's host can still remove it, forgetting
it when it cannot, queueing its removal (``move_cleanup_source``), the
operator's button and the hourly cron that removes the copies whose time
is up. The fields (``move_frozen_source_host_id``, ``move_frozen_until``)
stay with the move chain's other markers in ``cloud_instance.py``, and so
do the chain's hooks for the copy (``_move_freeze_steps``,
``_frozen_copy_cleanup_steps``).
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class CloudInstanceFrozenCopy(models.Model):
    _inherit = "cloud.instance"

    # ── The frozen copy a move leaves on the old host ──────────────────

    def _frozen_copy_reachable(self):
        """Whether the frozen copy's host can still be asked to remove it.

        A host archived, gone out of service or no longer compatible will
        not run the cleanup, and the copy the instance lives in is never
        a frozen one to remove.

        :rtype: bool
        """
        self.ensure_one()
        host = self.move_frozen_source_host_id
        return bool(
            host and host.active and host.status == "compatible"
            and host != self.host_id
        )

    def _forget_frozen_copy(self, why):
        """Clear the frozen-copy fields without reaching its host, and say so.

        For a copy no host will remove (``_frozen_copy_reachable``) or
        whose instance is about to go: the audit trail keeps where it was,
        for whoever reclaims the disk by hand.

        :param str why: what made it forgotten, for the audit row
        """
        self.ensure_one()
        host = self.move_frozen_source_host_id
        if not host:
            return
        self.env["cloud.audit.log"].sudo().create({
            "action": "Frozen copy forgotten",
            "instance_id": self.id,
            "host_id": host.id,
            "project_id": self.project_id.id or False,
            "details": f"Frozen copy on host {host.name} forgotten: {why}",
        })
        self.sudo().write({
            "move_frozen_source_host_id": False,
            "move_frozen_until": False,
        })

    def _enqueue_frozen_cleanup(self, why):
        """Queue the removal of the frozen copy, or forget it when it cannot run.

        The removal is a ``move_cleanup_source`` on the copy's host, in the
        instance's queue, so it runs before whatever is asked of the
        instance after it — the next move, its delete. One already waiting
        or running is reused. Queued with ``sudo``: the job type is the
        platform's, whoever asked for what needs it.

        :param str why: what asks for it, for the note when the copy is
            forgotten instead
        :return: the cleanup's job id, or False when the copy was forgotten
            or there is none
        """
        self.ensure_one()
        host = self.move_frozen_source_host_id
        if not host:
            return False
        if not self._frozen_copy_reachable():
            self._forget_frozen_copy(
                f"host {host.name} cannot be reached for {why}",
            )
            return False
        Job = self.env["cloud.job"].sudo()
        running = Job.search([
            ("instance_id", "=", self.id),
            ("host_id", "=", host.id),
            ("job_type_id.code", "=", "move_cleanup_source"),
            ("state", "in", Job._active_states),
        ], limit=1)
        if running:
            return running.id
        return Job.enqueue(host.id, self.id, "move_cleanup_source")

    def action_clean_frozen_copy(self):
        """Remove the frozen copy of the last move now (operator's button).

        :return: ``{'ok': True, 'job_id': <the cleanup, or None when the
            copy was forgotten because its host cannot run it>}``
        :raises UserError: when there is no frozen copy
        """
        self.ensure_one()
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        if not self.move_frozen_source_host_id:
            raise UserError(_("This instance has no frozen copy to clean."))
        job_id = self._enqueue_frozen_cleanup("a clean-up asked for by hand")
        return {"ok": True, "job_id": job_id or None}

    # Private: ``ir.cron`` runs it as the platform; nothing calls it
    # through ``call_kw``.
    @api.private
    @api.model
    def cron_clean_frozen_move_sources(self):
        """Remove the frozen copies whose time is up.

        Archived instances too: a copy outlives the record's archiving
        until it is removed. One whose instance is moving again is left to
        that move, which removes it first (``move_to_host``). A copy on a
        host that is archived, out of service or no longer compatible is
        forgotten, with a note in the audit trail.

        :return: how many cleanups were queued
        :rtype: int
        """
        due = self.with_context(active_test=False).search([
            ("move_frozen_source_host_id", "!=", False),
            ("move_frozen_until", "<=", fields.Datetime.now()),
            ("move_origin_host_id", "=", False),
        ])
        queued = 0
        for inst in due:
            host = inst.move_frozen_source_host_id
            try:
                with self.env.cr.savepoint():
                    if not inst._frozen_copy_reachable():
                        inst._forget_frozen_copy(
                            f"host {host.name} is retired or cannot be reached",
                        )
                        continue
                    if inst._enqueue_frozen_cleanup("its time was up"):
                        queued += 1
            except Exception as exc:  # noqa: BLE001
                # One instance whose queue refuses must not cost the
                # others their cleanup: the next pass tries again.
                _logger.warning(
                    "Could not queue the frozen copy cleanup of %s: %s",
                    inst.name, exc,
                )
        return queued
