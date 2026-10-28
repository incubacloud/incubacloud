"""The assistant's chat is one more channel of the MCP's approvals."""

from odoo import fields, models


class McpApproval(models.Model):
    """Approvals asked for by the assistant, answered with a card in the chat."""

    _inherit = "incubacloud.mcp.approval"

    channel = fields.Selection(
        selection_add=[("assistant", "Card in the panel's assistant")],
        ondelete={"assistant": "cascade"},
    )
