"""The branches an agent created, so writing to them asks no approval.

Writing to a branch that existed before asks the user (the branch rule of
the assistant's spec): a push to a branch an instance deploys from rebuilds
it, and production's would deploy to production. A branch the user's agent
created is the agent's work in progress; it is remembered per user, not
per credential, because the person approving is the same.
"""

from odoo import api, fields, models

from .cloud_audit_log import agent_mark


class McpAgentBranch(models.Model):
    """One branch an agent created for a user."""

    _name = "incubacloud.mcp.agent.branch"
    _description = "Branch an agent created for a user"
    _order = "id desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True
    )
    repository = fields.Char(
        required=True, index=True, readonly=True, help="owner/name, in lower case."
    )
    branch = fields.Char(required=True, readonly=True)
    via_client = fields.Char(readonly=True)

    _user_repository_branch = models.Constraint(
        "UNIQUE(user_id, repository, branch)",
        "A branch is remembered once per user.",
    )

    @api.model
    def _remember(self, repository, branch):
        """Remember that the current user's agent created *branch*.

        :param str repository: ``owner/name``
        """
        key = repository.lower()
        Branch = self.sudo()
        if not Branch.search_count(
            [
                ("user_id", "=", self.env.uid),
                ("repository", "=", key),
                ("branch", "=", branch),
            ],
            limit=1,
        ):
            Branch.create(
                {
                    "user_id": self.env.uid,
                    "repository": key,
                    "branch": branch,
                    "via_client": agent_mark(self.env),
                }
            )

    @api.model
    def _created_by_user(self, repository, branch):
        """Return whether the current user's agent created *branch*."""
        return bool(
            self.sudo().search_count(
                [
                    ("user_id", "=", self.env.uid),
                    ("repository", "=", repository.lower()),
                    ("branch", "=", branch),
                ],
                limit=1,
            )
        )
