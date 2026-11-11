"""The tools' handlers: one method per tool that needs more than its route.

Each group of tools (``mcp_tools_*.py``) extends this model with its
handlers and ``incubacloud.mcp.catalog`` with its specs. A spec with a
``route`` and no ``handler`` is served by :meth:`_invoke` alone.
"""

import json

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.cloud_rate_limit import consume_quota

from ..protocol.schema import SchemaError

#: The arguments that name a record, any tool's: the record's model, and
#: how a refusal names it. Checked before the tool runs
#: (:meth:`McpTools._check_records`), so that a record that does not
#: exist is said to be missing whatever the tool would have done with it
#: — a filter that matched nothing, a setting saved while the record it
#: named was ignored (6-nov-2026).
RECORD_ARGS = {
    "instance_id": ("cloud.instance", "instance"),
    "source_instance_id": ("cloud.instance", "instance"),
    "project_id": ("cloud.project", "project"),
    "host_id": ("cloud.host", "server"),
    "target_host_id": ("cloud.host", "server"),
    "job_id": ("cloud.job", "job"),
    "alert_id": ("cloud.alert", "alert"),
    "backend_id": ("cloud.backup.backend", "backup destination"),
    "grant_id": ("cloud.restore.upload.grant", "upload door"),
}

#: The update tools that change one record through ``vals``: the
#: argument naming it, its model. Their answer says which of the fields
#: given changed (:meth:`McpTools._invoke`): an update whose every value
#: was already there answered ``{"ok": true}`` as one that changed them.
TRACKED_UPDATES = {
    "update_instance": ("instance_id", "cloud.instance"),
    "update_project": ("project_id", "cloud.project"),
    "manage_host": ("host_id", "cloud.host"),
}

#: The keys of ``vals`` that replace a list of lines, the field holding
#: the lines and the fields that make one up.
LINE_KEYS = {
    "repos": ("repo_ids", ("url", "branch", "addons", "excludes", "commit_sha")),
    "domains": (
        "domain_ids",
        ("hostname", "redirect_to", "cert_resolver", "redirect_permanent"),
    ),
    "whitelist": ("whitelist_ids", ("hostname",)),
}


def _plain(value):
    """Return a field's value as something to compare: records by their ids."""
    if isinstance(value, models.BaseModel):
        return tuple(sorted(value.ids))
    return value


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
        """Run tool *spec* with its validated *args*; return its answer.

        An update of :data:`TRACKED_UPDATES` answers which of the fields
        it was given changed, under ``changed``, and says so when none
        did; it is refused when given no field at all.
        """
        tracked = TRACKED_UPDATES.get(spec.name)
        record = vals = None
        if tracked and args.get(tracked[0]) and "vals" in args:
            vals = args["vals"]
            if not vals:
                raise UserError(self.env._("Nothing to change: vals is empty."))
            record = (
                self.env[tracked[1]]
                .sudo()
                .with_context(active_test=False)
                .browse(args[tracked[0]])
            )
            before = self._snapshot(record, vals)
        if spec.handler:
            result = getattr(self, spec.handler)(args)
        else:
            result = self._route(spec.route, **args)
            result = self._with_job_state(result) if spec.enqueues else result
        if record is not None and isinstance(result, dict) and result.get("ok"):
            after = self._snapshot(record, vals)
            changed = sorted(key for key in before if before[key] != after.get(key))
            result = {**result, "changed": changed}
            if not changed:
                result["message"] = self.env._(
                    "Nothing changed: every value given was already set."
                )
        return result

    @api.model
    def _snapshot(self, record, vals):
        """Return what *record* holds for each key of *vals*, to compare.

        A key the record has no field for is left out of what is
        compared, and so is never said to have changed or not.

        :param record: the record an update tool writes, with ``sudo``
        :param dict vals: the tool's ``vals``
        :rtype: dict
        """
        self.env.flush_all()
        record.invalidate_recordset()
        if not record.exists():
            return {}
        values = {}
        for key in vals:
            if key in LINE_KEYS:
                field, line_fields = LINE_KEYS[key]
                if field in record._fields:
                    values[key] = [
                        tuple(_plain(line[name]) for name in line_fields)
                        for line in record[field]
                    ]
            elif key in record._fields:
                values[key] = _plain(record[key])
        return values

    @api.model
    def _check_records(self, args):
        """Refuse a call that names a record the user cannot reach.

        For the arguments of :data:`RECORD_ARGS`, at the top of the call:
        archived records exist. One that exists but that the user may not
        read, on a model they read, gets the same words as one that does
        not exist, so the answer does not tell it is there (as
        ``controllers/connect.py`` answers). On a model the user reads
        none of, only its existence is checked: the route says the rest.
        Whether the user may change the record is the route's to say, as
        in the panel.

        :raises SchemaError: naming the argument and the id, as the
            arguments' other checks do
        """
        for name, (model, label) in RECORD_ARGS.items():
            value = args.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            records = self.env[model].with_context(active_test=False)
            record = records.sudo().browse(value).exists().with_env(records.env)
            if record and records.has_access("read"):
                record = record._filtered_access("read")
            if not record:
                raise SchemaError(
                    f"arguments.{name}: no {label} you can reach has the id {value}."
                )

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
    def _charges(self, spec, args):
        """Return whether this call changes what an account is billed.

        Always for a tool declared as charging; for one that charges per
        call, what ``_charges_<tool name>`` says of the arguments, and yes
        when the group defines none or the arguments are not known (it
        fails closed, as ``_confirmation`` does).

        :param spec: the tool's :class:`ToolSpec`
        :param dict args: the validated (and prepared) arguments, or None
        :rtype: bool
        """
        if spec.charges:
            return True
        if not spec.charges_per_call:
            return False
        method = getattr(self, f"_charges_{spec.name}", None)
        return True if method is None or args is None else bool(method(args))

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
    def _recorded_args(self, spec, args):
        """Return the arguments as the panel records or shows them.

        The audit keeps them, and the assistant's card quotes them;
        ``_record_<tool name>`` takes out what neither may keep — a link
        that carries its own credential — when the group defines one.

        :param spec: the tool's :class:`ToolSpec`
        :param dict args: the validated (and prepared) arguments
        :rtype: dict
        """
        method = getattr(self, f"_record_{spec.name}", None)
        return method(args) if method else args

    @api.model
    def _shown_args(self, spec, args):
        """Return the arguments as a person approving the call reads them.

        ``_show_<tool name>`` when the group defines one — a link shown
        piece by piece, cut, so that a look-alike reads otherwise — and
        what the audit keeps (``_recorded_args``) for any other tool.

        :param spec: the tool's :class:`ToolSpec`
        :param dict args: the validated (and prepared) arguments
        :rtype: dict
        """
        method = getattr(self, f"_show_{spec.name}", None)
        return method(args) if method else self._recorded_args(spec, args)

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

        With :func:`consume_quota`, as every quota of a request: an
        agent's parallel calls do not fight over the bucket's row inside
        their transactions, a serialisation failure there cannot replay
        what came after, and at READ COMMITTED the second call waits for
        the row and adds to it, never passing the cap.

        :param str bucket: the rate-limit bucket
        :param str cap_key: the ``cloud.settings`` field holding the cap
        :param int max_per_window: a literal cap, without ``cap_key``
        :return: whether the hit is within the cap
        :rtype: bool
        """
        kwargs = (
            {"cap_key": cap_key} if cap_key else {"max_per_window": max_per_window}
        )
        return consume_quota(self.env, bucket, **kwargs)
