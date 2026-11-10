"""The audit says when an action came through an agent, and which."""

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.http import request

from ..protocol.context import (
    APPROVAL_CONTEXT_KEY,
    APPROVAL_REQUEST_ATTR,
    VIA_CONTEXT_KEY,
    VIA_REQUEST_ATTR,
)

#: How a call that needed an approval got it.
APPROVAL_SELECTION = [
    ("person", "Approved by the person"),
    ("auto", "Auto-approved by a rule"),
]
#: The fields the approval mark fills.
APPROVAL_FIELDS = ("approval", "approval_rule_id")


def agent_mark(env):
    """Return the agent what is being written comes through, or ``''``.

    In an HTTP request, only what the MCP endpoint left on the request
    counts, never the context, which a panel user sends with any RPC.
    Without a request, the context of *env*.
    """
    if request:
        return getattr(request, VIA_REQUEST_ATTR, "") or ""
    return env.context.get(VIA_CONTEXT_KEY) or ""


def approval_mark(env):
    """Return how the call being run was approved, or ``{}``.

    ``{"approval": "person" | "auto", "rule": <rule id, 0 for a person>}``,
    read as :func:`agent_mark` reads the agent: from the request when
    there is one, else from the context of *env*.

    :rtype: dict
    """
    if request:
        mark = getattr(request, APPROVAL_REQUEST_ATTR, None)
    else:
        mark = env.context.get(APPROVAL_CONTEXT_KEY)
    if not isinstance(mark, dict) or mark.get("approval") not in ("person", "auto"):
        return {}
    return mark


def set_approval_mark(mark):
    """Leave *mark* on the current request, or clear it with ``None``.

    Without a request there is nowhere to leave it: the call then writes
    no mark (tests that need one set the context key).

    :param dict mark: what :func:`approval_mark` returns, or None
    """
    if request:
        setattr(request, APPROVAL_REQUEST_ATTR, mark or None)


def stamp(env, vals_list):
    """Write the agent's and the approval's marks in *vals_list*, in place.

    What the request says wins; without a mark, one sent by anyone but
    the superuser is dropped: neither can be forged through an RPC.

    :param env: the environment of the ``create``
    :param list vals_list: the values of each record created
    """
    via = agent_mark(env)
    mark = approval_mark(env)
    for vals in vals_list:
        if via:
            vals["via_client"] = via
        elif not env.su:
            vals.pop("via_client", None)
        if mark:
            vals["approval"] = mark["approval"]
            vals["approval_rule_id"] = mark.get("rule") or False
        elif not env.su:
            for name in APPROVAL_FIELDS:
                vals.pop(name, None)


def refuse_mark_write(env, vals, message):
    """Refuse a write of the marks by anyone but the superuser.

    :raises AccessError: with *message*
    """
    if not env.su and ({"via_client", *APPROVAL_FIELDS} & set(vals)):
        raise AccessError(message)


class CloudAuditLog(models.Model):
    """Audit rows with the agent that caused them, and how it was approved."""

    _inherit = "cloud.audit.log"

    via_client = fields.Char(
        readonly=True,
        help="Set when the action came through the MCP endpoint: the "
        "verified client id of the token, or 'API key <name>', then "
        "between brackets the name the client declares (not verified).",
    )
    approval = fields.Selection(
        APPROVAL_SELECTION,
        readonly=True,
        help="How the agent's call that wrote the row was approved. Empty: it "
        "needed no approval, or the row is older than this field.",
    )
    approval_rule_id = fields.Many2one(
        "incubacloud.mcp.auto.approval",
        readonly=True,
        index=True,
        ondelete="set null",
        help="The auto-approval rule the row is about: the one that approved "
        "the call, or the one granted or revoked.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Stamp the rows of an agent's request; drop a forged stamp."""
        stamp(self.env, vals_list)
        return super().create(vals_list)

    def write(self, vals):
        """Only the platform writes the marks of a row."""
        refuse_mark_write(
            self.env,
            vals,
            self.env._("Only the platform marks an audit row as done by an agent."),
        )
        return super().write(vals)

    def _format(self):
        """Add the agent and the approval to each entry the panel lists."""
        rows = super()._format()
        by_id = {entry.id: entry for entry in self}
        for row in rows:
            entry = by_id.get(row["id"])
            row["via_client"] = (entry.via_client if entry else "") or ""
            row.update(entry._approval_info() if entry else _NO_APPROVAL)
        return rows

    def _approval_info(self):
        """Return how this row's call was approved, as the panel shows it.

        ``approval`` (``''``, ``person`` or ``auto``), and for ``auto`` the
        rule's id and who granted it.

        :rtype: dict
        """
        self.ensure_one()
        rule = self.sudo().approval_rule_id
        auto = self.approval == "auto"
        return {
            "approval": self.approval or "",
            "approval_rule_id": rule.id if auto and rule else None,
            "approval_granted_by": (
                rule.granted_by_id.name or "" if auto and rule else ""
            ),
        }


_NO_APPROVAL = {"approval": "", "approval_rule_id": None, "approval_granted_by": ""}
