"""The tools' handlers: one method per tool that needs more than its route.

Each group of tools (``mcp_tools_*.py``) extends this model with its
handlers and ``incubacloud.mcp.catalog`` with its specs. A spec with a
``route`` and no ``handler`` is served by :meth:`_invoke` alone.
"""

import json

from odoo import api, models


def quoted(value):
    """Return *value* as an approval shows what an agent or a user typed.

    Between double quotes, as JSON writes it, every character that does
    not print — line breaks, carriage returns, terminal escapes,
    bidirectional controls, unusual spaces — escaped: nothing in it can
    pass for the panel's own words, nor hide part of itself.

    :rtype: str
    """
    text = json.dumps(str(value), ensure_ascii=False)
    return "".join(
        char
        if char.isprintable()
        else (f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}")
        for char in text
    )


class McpTools(models.AbstractModel):
    """The tools' handlers, extended by each group of tools."""

    _name = "incubacloud.mcp.tools"
    _description = "Handlers of the MCP tools"

    @api.model
    def _invoke(self, spec, args):
        """Run tool *spec* with its validated *args*; return its answer."""
        if spec.handler:
            return getattr(self, spec.handler)(args)
        result = self._route(spec.route, **args)
        return self._with_job_state(result) if spec.enqueues else result

    @api.model
    def _confirmation(self, spec, args):
        """Return what the user must approve for this call, or None.

        ``_confirm_<tool name>`` says it when the group defines one. Fails
        closed: an ``always`` tool asks even when that method is missing
        or says nothing, and a ``conditional`` tool without the method
        asks every time.
        """
        if spec.confirm == "never":
            return None
        method = getattr(self, f"_confirm_{spec.name}", None)
        text = method(args) if method else None
        if text or (method and spec.confirm == "conditional"):
            return text or None
        return self.env._("Run the tool %s with these arguments.", spec.name)

    @api.model
    def _writes(self, spec, args):
        """Return whether this call may change something, and so is audited.

        A read-only tool never does; ``_writes_<tool name>`` decides for a
        tool that also reads, when the group defines one.
        """
        if spec.read_only:
            return False
        method = getattr(self, f"_writes_{spec.name}", None)
        return method(args) if method else True

    @api.model
    def _route(self, path, **params):
        """Call the panel route at *path* (see ``incubacloud.mcp.dispatch``)."""
        return self.env["incubacloud.mcp.dispatch"]._call(path, params)

    @api.model
    def _with_job_state(self, result):
        """Add the job's state and what it waits behind to *result*.

        ``deploy_instance`` and ``rebuild_instance`` already answer with it;
        every other route that enqueues answers ``job_id`` only, and the
        agent needs to know whether it started.
        """
        if (
            not isinstance(result, dict)
            or not result.get("job_id")
            or "queued_behind" in result
        ):
            return result
        state = self._route("/cloud/get_job_state", job_id=result["job_id"])
        if not state.get("ok"):
            return result
        return result | {key: value for key, value in state.items() if key != "ok"}

    @api.model
    def _consume_quota(self, bucket, cap_key=None, max_per_window=None):
        """Count one hit on *bucket* in a transaction of its own.

        Like ``_consume_github_quota``: an agent's parallel calls do not
        fight over the bucket's row inside their transactions, and a
        serialisation failure there cannot replay what came after.

        :return: whether the hit is within the cap
        :rtype: bool
        """
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            kwargs = (
                {"cap_key": cap_key} if cap_key else {"max_per_window": max_per_window}
            )
            allowed = env["cloud.rate.limit"].sudo().hit(bucket, **kwargs)
            cr.commit()
            return allowed
