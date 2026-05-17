from odoo import fields, models


class CloudInstancePendingPush(models.Model):
    """Push events that arrived while a rebuild was in flight or in cooldown.

    Created from ``cloud.github.event._process_push_event`` when an auto-rebuild
    cannot be triggered immediately. Consumed by
    ``rebuild_instance_executor.on_success``, which enqueues a coalesced
    rebuild carrying all queued pushes in its payload — guaranteeing the
    instance ends up at the latest HEAD and that every skipped push remains
    visible (first in the instance card, then in the resulting job).
    """

    _name = "cloud.instance.pending.push"
    _description = "Pending Push Awaiting Coalesced Rebuild"
    _order = "create_date asc, id asc"

    instance_id = fields.Many2one(
        "cloud.instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    event_id = fields.Many2one(
        "cloud.github.event",
        ondelete="set null",
        help="Source webhook event; cleared if the row is purged later.",
    )
    push_repo = fields.Char(string="Repository")
    push_branch = fields.Char(string="Branch")
    push_sha = fields.Char(string="Short SHA")
    push_message = fields.Char(string="Commit Message")
    push_by = fields.Char(string="Pusher")
    skip_reason = fields.Selection(
        [("cooldown", "Cooldown"), ("active_job", "Active Job")],
        required=True,
    )

    def _to_payload(self):
        """Serialize one record into a plain dict for ``cloud.job.payload``."""
        self.ensure_one()
        return {
            "push_repo": self.push_repo or "",
            "push_branch": self.push_branch or "",
            "push_sha": self.push_sha or "",
            "push_message": self.push_message or "",
            "push_by": self.push_by or "",
            "skip_reason": self.skip_reason or "",
            "queued_at": fields.Datetime.to_string(self.create_date) or "",
        }
