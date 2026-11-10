"""What an instance's database is doing now: its sessions, locks and tables.

Read on demand (``scripts/read_pg_activity.sh``) when somebody presses
"Queries now" on the instance's Metrics tab, and never stored: the text
of a query can carry personal data, so it reaches only the reader who
asked, who already holds the Developer role the tab needs.
"""
import json

from odoo import _, models
from odoo.exceptions import UserError

#: Fields kept from each session; anything else the script might add is
#: dropped rather than passed to the browser.
_SESSION_FIELDS = (
    "pid", "user", "application", "state", "wait_event_type", "wait_event",
    "age", "xact_age", "blocked_by", "blocks", "query",
)


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    def _parse_db_activity(self, raw):
        """Return the reading ``read_pg_activity.sh`` printed, checked.

        The script prints one JSON line; whatever the container said
        before it (a warning of the image's Python) is skipped.

        :param str raw: the script's standard output.
        :returns: ``{"summary", "sessions", "tables"}``.
        :rtype: dict
        :raises UserError: when there is no reading in it.
        """
        self.ensure_one()
        for line in reversed((raw or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict) or "summary" not in data:
                continue
            return {
                "summary": data.get("summary") or {},
                "sessions": [
                    {key: row.get(key) for key in _SESSION_FIELDS}
                    for row in data.get("sessions") or ()
                    if isinstance(row, dict)
                ],
                "tables": [
                    row for row in data.get("tables") or ()
                    if isinstance(row, dict)
                ],
            }
        raise UserError(_("The database of this instance could not be read."))
