"""``/cloud/mcp/auto_approval/*``: the auto-approval rules, for a person in the panel.

No tool reaches these routes (the catalog excludes them), and each refuses
a request that carries an agent's mark: an agent never sees, grants or
revokes a rule. Granting asks for an Administrator; revoking, for an
Administrator or the rule's own person.
"""

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from odoo.addons.incubacloud.controllers._client_ip import client_ip

from ..models.mcp_auto_approval import MAX_DAYS, NOTICE_VERSION, PERIOD_DAYS

#: The most activity rows one page holds.
ACTIVITY_PAGE_MAX = 100


def _int(value):
    """Return *value* as an id, or 0 when it is not a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


class McpAutoApprovalController(http.Controller):
    """The rules: list, candidates, grant, revoke and activity."""

    def _rules(self):
        """Return the rules model, refusing a request that comes through an agent."""
        Rule = request.env["incubacloud.mcp.auto.approval"]
        Rule._check_person()
        request.env["cloud.security.mixin"]._check_cloud_group("group_cloud_user")
        return Rule

    def _visible_rule(self, rule_id):
        """Return rule *rule_id* when the user may see it: an Administrator any,
        anyone else their own.

        :raises AccessError: for a rule that does not exist or is another's
        """
        Rule = self._rules()
        rule = Rule.sudo().browse(_int(rule_id)).exists()
        mixin = request.env["cloud.security.mixin"]
        if not rule or (
            rule.user_id.id != request.env.uid
            and not mixin._has_cloud_group("group_cloud_manager")
        ):
            raise AccessError(request.env._("No such auto-approval rule."))
        return rule

    @http.route("/cloud/mcp/auto_approval/list", type="jsonrpc", auth="user")
    def auto_approval_list(self):
        """The rules: every rule for an Administrator, the user's own otherwise."""
        Rule = self._rules()
        manager = request.env["cloud.security.mixin"]._has_cloud_group(
            "group_cloud_manager"
        )
        domain = [] if manager else [("user_id", "=", request.env.uid)]
        Settings = request.env["cloud.settings"].sudo()
        return {
            "rules": Rule.sudo().search(domain, limit=200)._format(),
            "can_grant": manager,
            "allowed": Settings._mcp_auto_approval_allowed(),
            "charges": Settings._mcp_auto_approval_charges(),
            "notice_version": NOTICE_VERSION,
            "periods": list(PERIOD_DAYS),
            "max_days": MAX_DAYS,
        }

    @http.route("/cloud/mcp/auto_approval/candidates", type="jsonrpc", auth="user")
    def auto_approval_candidates(self):
        """The users and credentials a rule may be granted to. Administrators."""
        Rule = self._rules()
        request.env["cloud.security.mixin"]._check_can_manage_settings()
        return {"users": Rule._candidates()}

    @http.route("/cloud/mcp/auto_approval/grant", type="jsonrpc", auth="user")
    def auto_approval_grant(
        self,
        user_id=None,
        credential="",
        days=None,
        expires_on=None,
        acknowledged=False,
        notice_version="",
    ):
        """Grant a rule, once the notice is accepted. Administrators."""
        Rule = self._rules()
        try:
            rule = Rule._grant(
                user_id,
                credential or "",
                days=days,
                expires_on=expires_on,
                acknowledged=acknowledged,
                notice_version=notice_version or "",
                ip=client_ip(),
            )
        except UserError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "rule": rule._format()[0]}

    @http.route("/cloud/mcp/auto_approval/revoke", type="jsonrpc", auth="user")
    def auto_approval_revoke(self, rule_id=None):
        """Revoke a rule: an Administrator any, anyone else their own."""
        rule = self._visible_rule(rule_id)
        rule.with_env(request.env)._revoke_by_person()
        return {"ok": True, "rule": rule._format()[0]}

    @http.route("/cloud/mcp/auto_approval/activity", type="jsonrpc", auth="user")
    def auto_approval_activity(self, rule_id=None, offset=0, limit=20):
        """A page of what a rule approved, with its grant and revocation."""
        rule = self._visible_rule(rule_id)
        offset = max(0, offset if isinstance(offset, int) else 0)
        limit = limit if isinstance(limit, int) else 20
        limit = min(max(1, limit), ACTIVITY_PAGE_MAX)
        return rule._activity(offset=offset, limit=limit)
