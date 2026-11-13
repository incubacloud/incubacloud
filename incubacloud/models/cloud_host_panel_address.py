"""The panel's outgoing address changed: which hosts may shut it out.

A hardened host lets SSH in from a list when it has one: its
``allowed_ssh_ips``, the panel's address when a layer knows it
(``_hardening_panel_ip``), and the address the hardening run itself came
from, which the play reads on the host (``SSH_CONNECTION``). With no list
at all SSH stays open to any address, key only. So a panel whose
outgoing address changes finds the hosts with a list closed to it, and
nothing said so until a job failed to connect.

Odoo.sh moves a project's production to another address now and then,
and tells it with a request to ``/_odoo.sh/ip-change`` (the old address
and the new one; ``controllers/odoo_sh``). ``_panel_address_changed``
keeps the change and raises one alert, critical, naming the hosts whose
list does not admit the new address (``panel_address_changed``). It
changes nothing on any host: the request carries no credential, and
opening SSH to an address on its word is what an attacker would ask for.

A host leaves the alert when its list admits the new address, when it is
hardened again after the change (the run admits the address it came
from, the new one), when it is archived or no longer hardened; the alert
closes when none is left. With no new address given, every hardened host
with a list is named, as nothing tells which of them admits it.
"""
import json
import logging

from odoo import _, api, fields, models

from .cloud_host import _ssh_address, _ssh_allow_entries, _ssh_source_network

_logger = logging.getLogger(__name__)

ALERT = "panel_address_changed"

#: The last change not settled yet: ``{"old", "new", "at"}`` as JSON,
#: removed once no host is left at risk.
CHANGE_PARAM = "incubacloud.panel_address_change"

#: The host fields whose change may settle a host.
_SETTLING_FIELDS = frozenset({"allowed_ssh_ips", "hardened", "hardened_at", "active"})


class CloudHost(models.Model):
    _inherit = "cloud.host"

    def write(self, vals):
        """Judge the pending address change again when a host may be settled.

        :param dict vals: the values written
        :return: what ``write`` returns
        """
        result = super().write(vals)
        if _SETTLING_FIELDS.intersection(vals) and self.env[
            "ir.config_parameter"
        ].sudo().get_param(CHANGE_PARAM):
            self.env["cloud.host"]._judge_panel_address_change()
        return result

    @api.model
    def _panel_address_changed(self, old, new=None):
        """Record that the panel's outgoing address changed, and judge the hosts.

        :param str old: the address the panel left with until now
        :param str new: the address it leaves with now, if known; anything
            that is not an address counts as unknown
        :return: whether a change was recorded (``False`` when both
            addresses are the same)
        :rtype: bool
        :raise ValueError: when *old* is not an address
        """
        old_ip = _ssh_address(old)
        if old_ip is None:
            raise ValueError("the old address is not an IP address")
        new_ip = _ssh_address(new)
        if new_ip == old_ip:
            return False
        change = {
            "old": str(old_ip),
            "new": str(new_ip) if new_ip else "",
            "at": fields.Datetime.to_string(fields.Datetime.now()),
        }
        _logger.warning(
            "The panel's outgoing address changed from %s to %s",
            change["old"], change["new"] or "an address not given",
        )
        self.env["ir.config_parameter"].sudo().set_param(
            CHANGE_PARAM, json.dumps(change),
        )
        self._judge_panel_address_change()
        return True

    @api.model
    def _judge_panel_address_change(self):
        """Raise, refresh or close the alert of the pending address change.

        Names the hosts :meth:`_panel_address_admitted` says no for. With
        none left, the alert closes and the change is forgotten.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        try:
            change = json.loads(ICP.get_param(CHANGE_PARAM) or "null")
        except ValueError:
            change = None
        Alert = self.env["cloud.alert"].sudo()
        if not isinstance(change, dict) or not change.get("old"):
            Alert.resolve_alert(ALERT)
            return
        since = fields.Datetime.to_datetime(change.get("at"))
        new_ip = _ssh_address(change.get("new"))
        hosts = self.sudo().search([("hardened", "=", True)]).filtered(
            lambda host: not host._panel_address_admitted(new_ip, since)
        )
        if not hosts:
            Alert.resolve_alert(ALERT)
            ICP.set_param(CHANGE_PARAM, False)
            return
        names = ", ".join(hosts.mapped("name"))
        if new_ip:
            message = _(
                "This panel's outgoing address changed from %(old)s to "
                "%(new)s. These hosts let SSH in only from a list without "
                "the new address, so the panel may no longer reach them: "
                "%(hosts)s. On each, let %(new)s in from your provider's "
                "console, then run 'Apply Hardening' from the panel, which "
                "keeps the address it comes from (RB-40). The panel changes "
                "no host by itself.",
                old=change["old"], new=str(new_ip), hosts=names,
            )
        else:
            message = _(
                "This panel's outgoing address changed from %(old)s to an "
                "address it was not told. These hosts let SSH in only from "
                "a list, so the panel may no longer reach them: %(hosts)s. "
                "Find the new address, let it in on each from your "
                "provider's console, then run 'Apply Hardening' from the "
                "panel, which keeps the address it comes from (RB-40). The "
                "panel changes no host by itself.",
                old=change["old"], hosts=names,
            )
        Alert.raise_alert(
            ALERT, message, level="critical",
            payload={
                "old": change["old"],
                "new": str(new_ip) if new_ip else "",
                "host_ids": hosts.ids,
            },
        )

    def _panel_address_admitted(self, new_ip, since):
        """Return whether this host still lets the panel in after the change.

        Yes when it is not hardened, when it was hardened after the change
        (the run admitted the address it came from), when it has no list
        (SSH open to any address) or a catch-all in it, and when its list,
        or the panel's address a layer knows, holds *new_ip*. An entry
        that is no address counts for nothing but still makes a list.

        :param new_ip: the new address (``ipaddress``), or ``None`` when
            unknown: then only the first answers can say yes
        :param since: when the change was recorded (naive UTC), or ``None``
        :rtype: bool
        """
        self.ensure_one()
        if not self.hardened:
            return True
        if since and self.hardened_at and self.hardened_at > since:
            return True
        panel = (self._hardening_panel_ip() or "").strip()
        entries = _ssh_allow_entries(self.allowed_ssh_ips)
        if panel:
            entries.append(panel)
        if not entries:
            return True
        networks = [
            network for network in map(_ssh_source_network, entries)
            if network is not None
        ]
        if any(network.prefixlen == 0 for network in networks):
            return True
        if new_ip is None:
            return False
        return any(
            network.version == new_ip.version and new_ip in network
            for network in networks
        )
