"""JSON routes the instance's Backups tab uses for continuous recovery."""
from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request


def _instance(instance_id):
    """The instance, as the caller may read it, or None."""
    inst = request.env["cloud.instance"].browse(int(instance_id or 0)).exists()
    if not inst:
        return None
    inst.check_access("read")
    return inst


def _job_or_error(call):
    """Run *call* and answer ``{'ok', 'job_id'}`` or its UserError."""
    try:
        job_id = call()
    except UserError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "job_id": job_id or False}


class PitrController(http.Controller):

    @http.route("/cloud/pitr/status", type="jsonrpc", auth="user")
    def pitr_status(self, instance_id):
        """What the tab shows: state, objective and the restorable window."""
        inst = _instance(instance_id)
        if not inst:
            return {"ok": False, "error": _("Instance not found")}
        return {"ok": True, **inst._pitr_status_vals()}

    @http.route("/cloud/pitr/enable", type="jsonrpc", auth="user")
    def pitr_enable(self, instance_id, objective=None):
        """Switch it on (manager): a rebuild, then archiving."""
        inst = _instance(instance_id)
        if not inst:
            return {"ok": False, "error": _("Instance not found")}
        return _job_or_error(lambda: inst.action_pitr_enable(objective))

    @http.route("/cloud/pitr/disable", type="jsonrpc", auth="user")
    def pitr_disable(self, instance_id):
        """Switch it off (manager)."""
        inst = _instance(instance_id)
        if not inst:
            return {"ok": False, "error": _("Instance not found")}
        return _job_or_error(inst.action_pitr_disable)

    @http.route("/cloud/pitr/set_objective", type="jsonrpc", auth="user")
    def pitr_set_objective(self, instance_id, objective):
        """Change the recovery objective (manager)."""
        inst = _instance(instance_id)
        if not inst:
            return {"ok": False, "error": _("Instance not found")}
        return _job_or_error(lambda: inst.action_pitr_set_objective(str(objective)))

    @http.route("/cloud/pitr/restore", type="jsonrpc", auth="user")
    def pitr_restore(self, instance_id, target, confirm_name):
        """Restore to a moment (manager), confirmed by typing the name."""
        inst = _instance(instance_id)
        if not inst:
            return {"ok": False, "error": _("Instance not found")}
        if (confirm_name or "").strip() != inst.name:
            return {
                "ok": False,
                "error": _("Type the instance's name to confirm the restore."),
            }
        return _job_or_error(lambda: inst.action_pitr_restore(target))
