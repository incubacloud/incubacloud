"""The panel's assistant as a third kind of credential of auto-approval.

The assistant has no key nor client: its credential, for a rule, is
:data:`ASSISTANT_CREDENTIAL`, "the panel's assistant, for this person".
A rule of a key or a client never covers the assistant, nor the reverse.
"""

from odoo import api, fields, models

#: The credential key of the assistant's rules.
ASSISTANT_CREDENTIAL = "assistant"


def _end_assistant_rules(rules):
    """End the assistant's live rules when this module is uninstalled.

    Rules are history and cannot be deleted (``unlink`` refuses), so the
    live ones are revoked as a deleted credential's, and every rule stays
    with its audit rows.

    :param rules: the rules of kind ``assistant``
    """
    rules._revoke("credential deleted")


class McpAutoApproval(models.Model):
    """Rules may be granted to the panel's assistant of a person."""

    _inherit = "incubacloud.mcp.auto.approval"

    kind = fields.Selection(
        selection_add=[("assistant", "Panel assistant")],
        ondelete={"assistant": _end_assistant_rules},
    )

    @api.model
    def _candidates_for(self, user):
        """Add the panel's assistant, for a user who may use it now."""
        result = super()._candidates_for(user)
        Settings = self.env["cloud.settings"].sudo()
        if Settings._assistant_available_for(user):
            result.append(
                {
                    "credential": ASSISTANT_CREDENTIAL,
                    "kind": "assistant",
                    "label": self.env._("Panel assistant"),
                    "client_name": "",
                    "last_seen": "",
                }
            )
        return result
