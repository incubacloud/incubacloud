"""Read one instance's slice of the proxy access log.

Metrics tell you an instance is being hammered: requests per second,
status codes, latency. They cannot tell you *what* is being done to it,
because they carry neither the client IP nor the path — those only exist
in the access log. Without them you can detect an attack but not block
it, which is the difference between an alert and an answer.

Deliberately read on demand over SSH rather than shipped anywhere:

* the data stays on the host that produced it, which matters because
  these lines contain the IP addresses of somebody else's end users;
* it reuses the panel's existing SSH path, so there is no new central
  component to secure, size or keep alive;
* and it is enough for the question being asked — "who is hitting this
  instance right now" — which is a live question, not a historical one.

Centralised search, history and alerting on error rates need a log
pipeline, and that is a separate layer with its own isolation problem.

Each line carries ``RouterName`` and ``ServiceName`` in full (for example
``acme-prod-19-0-prod-main@docker``), which is the SAME key the metric
relabelling joins on: the panel feeds copier the project name it also
forces into COMPOSE_PROJECT_NAME. So attribution needs no domain lookup
and survives custom domains, redirects and multi-domain instances.
"""
import json
import logging
from operator import itemgetter

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: Compose project and service of the shared inverse proxy, as deployed
#: by ``full_setup``. Its container name follows compose's convention.
_PROXY_CONTAINER = "inverseproxy-proxy-1"

#: Hard ceiling on how much of the container log we pull per request. The
#: proxy logs every request for every instance on the host, so the slice
#: for one instance is a fraction of this — generous enough to be useful
#: during an incident, bounded enough not to move megabytes per click.
_MAX_TAIL = 5000

#: Fields kept from each line. Everything else is noise for this view,
#: and not copying it is one less way to leak something by accident.
_KEEP = (
    "time", "ClientHost", "RequestMethod", "RequestPath", "RequestHost",
    "DownstreamStatus", "Duration", "RouterName", "ServiceName",
)


class CloudInstance(models.Model):
    _inherit = "cloud.instance"

    def _access_log_command(self, tail=_MAX_TAIL):
        """Return the shell command that dumps the proxy's recent log.

        :param tail: how many lines of container output to read.
        :return: a command string safe to run over SSH.
        """
        self.ensure_one()
        tail = max(1, min(int(tail or _MAX_TAIL), _MAX_TAIL))
        return f"docker logs --tail {tail} {_PROXY_CONTAINER} 2>&1"

    def _access_log_matches(self, entry):
        """Return True when a parsed log line belongs to this instance.

        Matched on the router/service name rather than the Host header:
        the names are derived from panel state, whereas a host can serve
        several domains and redirect between them.
        """
        self.ensure_one()
        names = self._access_log_names()
        if not names:
            return False
        for field in ("ServiceName", "RouterName"):
            value = entry.get(field) or ""
            for name in names:
                if value.startswith(name) or value.startswith(f"https-{name}"):
                    return True
        return False

    def _access_log_names(self):
        """Return the Traefik name prefixes this instance answers under.

        Core knows the one derived from the copier project name. The SaaS
        layer overrides this to add the router it puts in front for
        sleep/wake, because only it knows that router exists.
        """
        self.ensure_one()
        name = self.doodba_project_name or self.name or ""
        version = (self.odoo_version or "").replace(".", "-")
        if not (name and version):
            return []
        return [f"{name}-{version}-"]

    def _parse_access_log(self, raw, limit=200):
        """Turn raw container output into this instance's recent requests.

        Non-JSON lines are skipped in silence: the proxy's own operational
        messages share the stream, and an unparseable line is not an
        error worth surfacing here.

        :param raw: the container log as one string.
        :param limit: most recent entries to return.
        :return: list of dicts, newest last.
        """
        self.ensure_one()
        rows = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not self._access_log_matches(entry):
                continue
            rows.append({key: entry.get(key) for key in _KEEP})
        return rows[-limit:]

    def _access_log_summary(self, rows):
        """Summarise a slice of requests the way an incident is triaged.

        Answers the three questions actually asked when an instance is
        under load: how many requests, which status codes, and who is
        sending the most.

        :param rows: parsed entries from ``_parse_access_log``.
        :return: dict with totals, status counts and the top clients.
        """
        self.ensure_one()
        by_status, by_client, by_path = {}, {}, {}
        for row in rows:
            status = str(row.get("DownstreamStatus") or "?")
            by_status[status] = by_status.get(status, 0) + 1
            client = row.get("ClientHost") or "?"
            by_client[client] = by_client.get(client, 0) + 1
            path = row.get("RequestPath") or "?"
            by_path[path] = by_path.get(path, 0) + 1

        def _top(counter, size=5):
            return [
                {"key": key, "count": count}
                for key, count in sorted(
                    counter.items(), key=itemgetter(1), reverse=True,
                )[:size]
            ]

        return {
            "total": len(rows),
            "by_status": _top(by_status, 8),
            "top_clients": _top(by_client),
            "top_paths": _top(by_path),
        }

    def _require_access_log_host(self):
        """Return the host to read from, or explain why there is none."""
        self.ensure_one()
        if not self.host_id:
            raise UserError(_(
                "This instance is not placed on a host yet, so there is no "
                "proxy log to read."
            ))
        return self.host_id
