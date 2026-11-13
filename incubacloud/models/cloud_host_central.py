"""Which servers reach the central under its single name, and which do not.

With the single name in force and only the panel's servers admitted
(``metrics_central_write_access`` = ``servers``), a server whose traffic
leaves with an address the central's host does not admit cannot write:
the port refuses it before any credential is read. Two signs say so, and
either raises ``metrics_central_writer_outside`` for that server:

* its own check: the agents' install posts to the central's write URL
  from the server, with the authority, and expects vmauth's 401; and it
  asks the panel which address it leaves with (``/cloud/egress_ip``);
* its silence: installed on the single name, nothing from it for 20
  minutes while other servers write, unless the panel cannot reach it
  either or its own check got through with the address the panel has
  (``_central_silence_explained``).

The alert names the address it leaves with, the one the panel has, and
the two ways out: correct the address in the panel, or let any address
write (where the panel may choose).
"""
import ipaddress
import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

#: A server installed on the single name and quiet this long, while
#: others write, is said to be outside (as the direct path's silence).
_SILENT_AFTER = timedelta(minutes=20)

#: How often the 15-minute cron asks the central's host to check a name
#: still pending: the sync job's own cron does it hourly.
_PENDING_CHECK_EVERY = timedelta(minutes=10)

#: What a server's check answers when it reached vmauth: refused for want
#: of a credential, which is the right answer without one.
_REACHED = 401

ALERT = "metrics_central_writer_outside"


def _egress_address(value):
    """Return *value* as the IPv4 address it names, or ''.

    What the server was told by ``/cloud/egress_ip``. The central's name
    has an A record only, so an IPv6 address is not the one the port
    judges; and a page a CDN answered instead (a challenge) is not an
    address at all. Neither is kept, nor shown in the alert.

    :param str value: what the server's ask printed.
    :rtype: str
    """
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return ""
    return str(address) if address.version == 4 else ""


class CloudHost(models.Model):
    _inherit = "cloud.host"

    metrics_egress_ip = fields.Char(
        string="Leaves with address",
        readonly=True,
        copy=False,
        help="The address this server's traffic leaves with, as the panel "
             "saw it at the agents' last install.",
    )
    metrics_central_reach = fields.Integer(
        string="Central answered the server with",
        readonly=True,
        copy=False,
        help="HTTP status the central's write URL answered this server at "
             "the agents' last install, without a credential: 401 is "
             "right; -1 means no answer.",
    )
    metrics_central_reach_at = fields.Datetime(
        string="Central last checked from the server", readonly=True, copy=False,
    )

    def _central_outside_message(self):
        """Say why this server cannot write to the central, and the ways out.

        :rtype: str
        """
        self.ensure_one()
        Settings = self.env["cloud.settings"].sudo()
        egress = self.metrics_egress_ip or _("an address the panel could not learn")
        message = _(
            '"%(host)s" cannot write to the metrics central: it leaves with '
            "%(egress)s and the panel has %(address)s for it, and the "
            "central admits only the panel's servers by their address. "
            "Correct the server's address in the panel.",
            host=self.name, egress=egress, address=self.ip_address or "?",
        )
        if not Settings._central_write_access_fixed():
            message += " " + _(
                "Or, if its address changes or it is behind NAT, set Who can "
                "write to the central to Any address in Settings › Monitoring."
            )
        return message

    def _central_outside_applies(self):
        """Whether the single name is in force with writers limited by address.

        :rtype: bool
        """
        Settings = self.env["cloud.settings"].sudo()
        return bool(
            Settings._central_agents_ca()
            and Settings._central_write_access() == "servers"
        )

    def _record_central_reach(self, status, egress):
        """Keep what the server's own check of the central said, and judge it.

        401 is the central reached; a 403 or no answer is the port
        refusing the server, which the alert names with the address it
        leaves with. Another status (a 5xx) is the central's, not the
        server's, and says nothing about the list.

        :param int status: what the write URL answered, -1 for nothing.
        :param str egress: the address the panel saw the server with;
            anything but an IPv4 address is dropped (``_egress_address``).
        """
        self.ensure_one()
        egress = _egress_address(egress)
        self.sudo().write({
            "metrics_central_reach": status,
            "metrics_central_reach_at": fields.Datetime.now(),
            "metrics_egress_ip": egress or False,
        })
        Alert = self.env["cloud.alert"].sudo()
        if status == _REACHED or not self._central_outside_applies():
            Alert.resolve_alert(ALERT, host=self)
        elif status in (403, -1):
            Alert.raise_alert(ALERT, self._central_outside_message(), host=self)

    def _central_silence_explained(self):
        """Whether this server's silence says nothing about the central's list.

        Its host does not answer the panel either (``host_unreachable``
        open: switched off, or its network down), or its own last check
        reached vmauth (401) leaving with the address the panel has for
        it, which the list admits. The alert would then blame the list
        for what it did not do, so the silence alone raises nothing; an
        alert its own check raised stays.

        :rtype: bool
        """
        self.ensure_one()
        if self.env["cloud.alert"].sudo().search_count([
            ("code", "=", "host_unreachable"), ("state", "=", "active"),
            ("host_id", "=", self.id),
        ], limit=1):
            return True
        return bool(
            self.metrics_central_reach == _REACHED
            and (self.metrics_egress_ip or "") in ("", (self.ip_address or "").strip())
        )

    @api.model
    def _reconcile_central_name(self):
        """Check a pending single name again, and look for servers left out.

        Called by the observability cron (every 15 minutes). A name not in
        force yet is checked from the central's host, through
        ``sync_direct_path``, when its last check is over ten minutes old.

        :return: how many servers are said to be outside.
        :rtype: int
        """
        Settings = self.env["cloud.settings"].sudo()
        settings = Settings._get_system()
        if not settings.metrics_enabled:
            return 0
        central = settings.metrics_central_host_id.sudo()
        now = fields.Datetime.now()
        if (
            Settings._central_single_path()
            and not Settings._central_single_in_force()
            and central and central.active and central.direct_port_shipped
        ):
            checked = settings.metrics_central_name_checked_at
            if not checked or now - checked >= _PENDING_CHECK_EVERY:
                central._enqueue_direct_sync("central's name pending")
        return self._central_silent_writers(now)

    @api.model
    def _central_silent_writers(self, now):
        """Raise the alert for servers on the single name gone quiet, close the rest.

        Quiet means nothing for ``_SILENT_AFTER`` while at least one other
        server on the same URL wrote: all of them quiet at once is the
        central, not the servers. Only a server installed with the URL in
        force counts; one not reapplied yet is the reconciliation's.

        :param datetime now: the cron's time.
        :return: how many servers are said to be outside.
        :rtype: int
        """
        Alert = self.env["cloud.alert"].sudo()
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not self._central_outside_applies():
            for alert in Alert.search([("code", "=", ALERT), ("state", "=", "active")]):
                Alert.resolve_alert(ALERT, host=alert.host_id)
            return 0
        url = (settings.metrics_remote_write_url or "").strip()
        hosts = self.sudo().search([
            ("metrics_agents_state", "=", "installed"), ("active", "=", True),
            ("metrics_agents_write_url", "=", url),
        ])

        def quiet(host):
            seen = host.metrics_last_seen
            return not seen or seen < now - _SILENT_AFTER

        writing = hosts.filtered(lambda h: not quiet(h))
        outside = 0
        for host in hosts:
            if not quiet(host):
                # Writing: inside the list, whatever its last check said.
                Alert.resolve_alert(ALERT, host=host)
                continue
            since = host.metrics_agents_since
            if since and since > now - _SILENT_AFTER:
                continue
            if not (writing - host):
                continue
            if host._central_silence_explained():
                continue
            Alert.raise_alert(ALERT, host._central_outside_message(), host=host)
            outside += 1
        return outside
