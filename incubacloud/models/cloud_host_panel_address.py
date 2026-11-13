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
A new address that is not a public one counts as unknown, as Odoo.sh's
production leaves with a public one; and the alert asks to check the
address against what Odoo.sh shows the project's administrators.

The list judged is the one the host confirmed: what a successful
hardening run applied, or what the host answered to a live change of its
list (``_ssh_allowlist_change``). An edit of ``allowed_ssh_ips`` by hand
reaches the host's firewall only with the next hardening run, so until
then (``ssh_allowlist_unconfirmed``) what the host lets in is unknown,
and the host is named.

A host leaves the alert when it is hardened again after the change (the
run admits the address it came from, the new one), when the list it
confirmed admits the new address, when it is archived, deleted or no
longer hardened; the alert closes when none is left. The change is then
kept, settled, so the same notice again does not raise it anew. With no
new address given, every hardened host with a list is named, as nothing
tells which of them admits it.
"""
import json
import logging

from odoo import _, api, fields, models

from .cloud_host import (
    SSH_ALLOWLIST_CONFIRMED,
    _ssh_address,
    _ssh_allow_entries,
    _ssh_source_network,
)

_logger = logging.getLogger(__name__)

ALERT = "panel_address_changed"

#: The last change: ``{"old", "new", "at", "settled"}`` as JSON. Kept
#: once no host is left at risk, ``settled``, so that Odoo.sh sending the
#: same notice again does not raise the alert again.
CHANGE_PARAM = "incubacloud.panel_address_change"

#: The host fields whose change may settle a host.
_SETTLING_FIELDS = frozenset({
    "allowed_ssh_ips", "ssh_allowlist_unconfirmed", "hardened", "hardened_at",
    "active",
})


class CloudHost(models.Model):
    _inherit = "cloud.host"

    ssh_allowlist_unconfirmed = fields.Boolean(
        string="SSH List Edited Since Applied",
        readonly=True,
        copy=False,
        help="Whether Allowed SSH IPs was edited after the host last "
             "confirmed its SSH list, with a successful hardening run or a "
             "live change of the list. What the host lets in may then differ "
             "from the field until the hardening is re-applied.",
    )

    def write(self, vals):
        """Mark an edit of the SSH list by hand, and judge the pending change.

        A write of ``allowed_ssh_ips`` is an edit by hand unless it keeps
        what the host itself answered (context key
        ``SSH_ALLOWLIST_CONFIRMED``, set by ``_ssh_allowlist_change``).

        :param dict vals: the values written
        :return: what ``write`` returns
        """
        if "allowed_ssh_ips" in vals and "ssh_allowlist_unconfirmed" not in vals:
            vals = dict(
                vals,
                ssh_allowlist_unconfirmed=not self.env.context.get(
                    SSH_ALLOWLIST_CONFIRMED,
                ),
            )
        result = super().write(vals)
        if _SETTLING_FIELDS.intersection(vals):
            self.env["cloud.host"]._judge_pending_panel_address_change()
        return result

    def unlink(self):
        """Judge the pending address change again once the hosts are gone.

        :return: what ``unlink`` returns
        """
        result = super().unlink()
        self.env["cloud.host"]._judge_pending_panel_address_change()
        return result

    @api.model
    def _panel_address_changed(self, old, new=None):
        """Record that the panel's outgoing address changed, and judge the hosts.

        The same change as the last one recorded is ignored, settled or
        not. A change that follows another still open closes its alert
        before raising one again, so that it is notified anew. The change
        is kept with one write of its parameter.

        :param str old: the address the panel left with until now
        :param str new: the address it leaves with now, if known; anything
            that is not a public address counts as unknown
        :return: whether a change was recorded (``False`` when both
            addresses are the same, or for the last change again)
        :rtype: bool
        :raise ValueError: when *old* is not an address
        """
        old_ip = _ssh_address(old)
        if old_ip is None:
            raise ValueError("the old address is not an IP address")
        new_ip = _ssh_address(new)
        if new_ip == old_ip:
            return False
        if new_ip is not None and not new_ip.is_global:
            new_ip = None
        change = {"old": str(old_ip), "new": str(new_ip) if new_ip else ""}
        last = self._panel_address_change_kept()
        if last and (last["old"], last.get("new") or "") == (change["old"], change["new"]):
            return False
        change["at"] = fields.Datetime.to_string(fields.Datetime.now())
        _logger.warning(
            "The panel's outgoing address changed from %s to %s",
            change["old"], change["new"] or "an address not given",
        )
        self.env["cloud.alert"].sudo().resolve_alert(ALERT)
        change["settled"] = not self._judge_panel_address_change(change)
        self.env["ir.config_parameter"].sudo().set_param(
            CHANGE_PARAM, json.dumps(change),
        )
        return True

    @api.model
    def _panel_address_change_kept(self):
        """Return the last address change recorded.

        :return: ``{"old", "new", "at", "settled"}``, or ``None`` when
            none was recorded (or what is kept cannot be read)
        :rtype: dict | None
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(CHANGE_PARAM)
        try:
            change = json.loads(raw or "null")
        except ValueError:
            return None
        if not isinstance(change, dict) or not change.get("old"):
            return None
        return change

    @api.model
    def _judge_pending_panel_address_change(self):
        """Judge the hosts again for the last change, if it is not settled.

        Called when a host may have been settled; marks the change settled
        once no host is left at risk.
        """
        change = self._panel_address_change_kept()
        if not change or change.get("settled"):
            return
        if not self._judge_panel_address_change(change):
            self.env["ir.config_parameter"].sudo().set_param(
                CHANGE_PARAM, json.dumps(dict(change, settled=True)),
            )

    @api.model
    def _judge_panel_address_change(self, change):
        """Raise, refresh or close the alert of *change*.

        Names the hosts :meth:`_panel_address_admitted` says no for,
        archived ones never, whatever the context; with none, the alert
        closes. The change is not written here.

        :param dict change: ``{"old", "new", "at"}`` as kept
        :return: the hosts named (empty when none is left)
        :rtype: cloud.host
        """
        Alert = self.env["cloud.alert"].sudo()
        since = fields.Datetime.to_datetime(change.get("at"))
        new_ip = _ssh_address(change.get("new"))
        hosts = self.sudo().with_context(active_test=True).search(
            [("hardened", "=", True)],
        ).filtered(lambda host: not host._panel_address_admitted(new_ip, since))
        if not hosts:
            Alert.resolve_alert(ALERT)
            return hosts
        names = ", ".join(hosts.mapped("name"))
        if new_ip:
            message = _(
                "This panel's outgoing address changed from %(old)s to "
                "%(new)s. These hosts let SSH in only from a list without "
                "the new address, or from one edited since they last "
                "applied it, so the panel may no longer reach them: "
                "%(hosts)s. First check %(new)s against Odoo.sh's email or "
                "the project's page on Odoo.sh: this notice proves nothing "
                "by itself. Then, on each, let it in from your provider's "
                "console and run 'Apply Hardening' from the panel, which "
                "keeps the address it comes from (RB-41). The panel changes "
                "no host by itself.",
                old=change["old"], new=str(new_ip), hosts=names,
            )
        else:
            message = _(
                "This panel's outgoing address changed from %(old)s to an "
                "address it was not told. These hosts let SSH in only from "
                "a list, so the panel may no longer reach them: %(hosts)s. "
                "Find the new address in Odoo.sh's email or on the "
                "project's page on Odoo.sh, let it in on each from your "
                "provider's console, then run 'Apply Hardening' from the "
                "panel, which keeps the address it comes from (RB-41). The "
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
        return hosts

    def _panel_address_admitted(self, new_ip, since):
        """Return whether this host still lets the panel in after the change.

        Yes when it is not hardened, when it was hardened after the change
        (the run admitted the address it came from). No when its list was
        edited by hand since it last confirmed it: what it lets in is then
        unknown. Otherwise yes when it has no list (SSH open to any
        address) or a catch-all in it, and when its list, or the panel's
        address a layer knows, holds *new_ip*. An entry that is no address
        counts for nothing but still makes a list.

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
        if self.ssh_allowlist_unconfirmed:
            return False
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
