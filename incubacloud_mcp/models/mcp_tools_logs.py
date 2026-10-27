"""Logs and captured mail: data from the instance, never instructions."""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.limits import page_items, page_record
from ..protocol.tool_spec import (
    LIMIT,
    OFFSET,
    ToolSpec,
    integer,
    obj,
    record_id,
    string,
)
from .mcp_catalog import tool_policies

_DEVELOPER = "group_cloud_developer"

TOOLS = (
    ToolSpec(
        "read_logs",
        "An instance's logs: source=live tails a service (Odoo's own log by "
        "default); archives lists the daily archives; archive reads one (name), "
        "optionally filtered (search); search counts a term (term) across the "
        "archives. Lines come in pages.",
        _DEVELOPER,
        obj(
            {
                "instance_id": record_id("The instance."),
                "source": string(
                    "Where to read.", enum=("live", "archives", "archive", "search")
                ),
                "service": string("Compose service for source=live.", max_length=63),
                "lines": integer(
                    "How many lines to fetch from the server.", minimum=1, maximum=2000
                ),
                "name": string("Archive file for source=archive.", max_length=200),
                "search": string("Filter for source=archive.", max_length=200),
                "term": string("Term for source=search.", max_length=200),
                "max_files": integer(
                    "Archives to scan for source=search.", minimum=1, maximum=60
                ),
                "offset": OFFSET,
                "limit": LIMIT,
            },
            required=("instance_id",),
        ),
        handler="_tool_read_logs",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "staging_mailbox",
        "The mail a staging caught instead of sending: the list, or one message "
        "(message_id), whose text and HTML come in pages. Links with tokens are "
        "replaced.",
        _DEVELOPER,
        obj(
            {
                "instance_id": record_id("The staging."),
                "message_id": string("One message.", max_length=200),
                "offset": OFFSET,
                "limit": LIMIT,
            },
            required=("instance_id",),
        ),
        handler="_tool_staging_mailbox",
        read_only=True,
        untrusted=True,
    ),
)

ROUTES = {
    "/cloud/fetch_container_logs": "read_logs",
    "/cloud/instance_log_archives": "read_logs",
    "/cloud/fetch_log_archive": "read_logs",
    "/cloud/search_log_archives": "read_logs",
    "/cloud/instance_mailbox": "staging_mailbox",
    "/cloud/instance_mail": "staging_mailbox",
}


class McpCatalogLogs(models.AbstractModel):
    """The log and mail tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the log and mail tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the log and mailbox routes."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsLogs(models.AbstractModel):
    """Handlers of the log and mail tools.

    Links that carry a token are replaced by ``_run``, as for every
    ``untrusted`` tool.
    """

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_read_logs(self, args):
        """Read logs the way the instance's Logs tab does, paged."""
        instance_id = args["instance_id"]
        source = args.get("source") or "live"
        if source == "archives":
            return self._route("/cloud/instance_log_archives", instance_id=instance_id)
        if source == "search":
            if not args.get("term"):
                raise UserError(self.env._("Give the term to search for."))
            return self._route(
                "/cloud/search_log_archives",
                instance_id=instance_id,
                term=args["term"],
                max_files=args.get("max_files") or 60,
            )
        if source == "archive":
            if not args.get("name"):
                raise UserError(self.env._("Name the archive to read."))
            result = self._route(
                "/cloud/fetch_log_archive",
                instance_id=instance_id,
                name=args["name"],
                lines=args.get("lines") or 500,
                search=args.get("search") or "",
            )
        else:
            result = self._route(
                "/cloud/fetch_container_logs",
                instance_id=instance_id,
                service=args.get("service") or "odoo",
                lines=args.get("lines") or 200,
            )
        if result.get("ok") is False:
            return result
        return page_items(
            result.get("lines") or [], args.get("offset"), args.get("limit")
        )

    @api.model
    def _tool_staging_mailbox(self, args):
        """A staging's captured mail: the list, or one message, in 64 KB pages.

        One message's text and HTML are paged as one (``page_record``); the
        list is paged by messages, the host reading from ``offset``.
        """
        offset = args.get("offset") or 0
        if args.get("message_id"):
            result = self._route(
                "/cloud/instance_mail",
                instance_id=args["instance_id"],
                message_id=args["message_id"],
            )
            if result.get("ok") is False:
                return result
            return page_record(result, ("text", "html"), offset)
        result = self._route(
            "/cloud/instance_mailbox",
            instance_id=args["instance_id"],
            limit=args.get("limit"),
            start=offset,
        )
        if result.get("ok") is False:
            return result
        items = result.get("items") or []
        page = page_items(items, 0, None)
        following = offset + len(page["items"])
        total = result.get("total") or 0
        more = following < max(total, offset + len(items))
        return {
            **{key: value for key, value in result.items() if key != "items"},
            "items": page["items"],
            "offset": offset,
            "truncated": page["truncated"] or more,
            "next_offset": following if more else None,
        }
