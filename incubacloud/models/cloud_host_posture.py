"""Whether a host is as closed, and as hardened, as the panel says.

Refusing direct traffic (``block_direct_access``) has two halves: Traefik
answers 403 to whatever skipped the trusted proxies, and the firewall
drops it before it gets there. The first lands with Full Setup or a
proxy push; the second only with a hardening run, which nothing starts
by itself. So the setting can say "closed" while the origin answers
anybody who knows its address -- a host joining a pool after its
hardening, or ticked by hand, stays that way until someone re-applies
the hardening. The same goes for the recipe: a change to the hardening
playbook reaches a host only when it is hardened again.

On 27 October 2026 one host behind the CDN had been open at its origin
for nineteen days, and none had taken the hardening changes of the
twelve days before. Nothing in the panel said so: the posture had been
taken for granted instead of checked. This hourly check asks the two
questions instead:

* **Does a host said to admit only its proxies drop a direct
  connection?** (``host_origin_open``) The panel connects straight to
  the host's address, asking for a name it serves -- the panel's own
  ``curl --resolve name:443:ip`` -- on 443 and then 80. A closed origin
  lets nothing in; anything that gets in, a page or Traefik's 403 alike,
  means the firewall half is missing. Only a connection left unanswered
  until the wait runs out reads as closed: one refused, or an address
  unreachable, is no reading, and the host is left as it was until a
  run gets one. A measurement over the network, so
  ``cloud.alert.policy`` confirms it over two runs. It runs from this
  panel, never through the CDN. Silence is the healthy reading here, so
  a panel whose own egress drops everything -- out only through a
  proxy, or inside a perimeter that discards direct connections --
  would read every host as closed: a drop counts as closed only once
  the same name answers through the CDN, and a host serving no name
  gets no reading from it.
* **Is its hardening the recipe and the posture wanted now?**
  (``host_hardening_outdated``) What the last successful run applied
  (``hardening_recipe``, ``hardening_cdn_only``) against the recipe this
  panel ships and the firewall the host's settings ask for. Read from
  the records, so it raises on the first run.

Both alert, neither acts: re-applying the hardening touches a live host,
and that stays the operator's decision. Both clear themselves once the
host is what it is said to be.
"""
import logging
import socket
import ssl

from odoo import _, api, models

from .registry import executor_registry

_logger = logging.getLogger(__name__)

ORIGIN_OPEN_ALERT_CODE = "host_origin_open"
HARDENING_OUTDATED_ALERT_CODE = "host_hardening_outdated"

#: Seconds to wait for a direct connection, and then for its answer.
#: Short: a closed origin drops the connection, so each host it checks
#: costs this much on each port, and an open one answers well within it.
_ORIGIN_PROBE_TIMEOUT = 5

#: The address of an on-demand host its provider has not handed over yet.
_UNASSIGNED_IP = "0.0.0.0"


def _origin_answer(ip, port, name, tls, timeout=_ORIGIN_PROBE_TIMEOUT):
    """Return what *ip* answers on *port* to a request for *name*.

    The connection goes straight to the address, around the DNS and so
    around the CDN, and the request names *name* as a visitor's would
    (SNI and ``Host``). Only the connection is the verdict: a firewall
    that admits nothing but the CDN drops it, and nothing comes back
    until the wait runs out. A refusal is no verdict: the reset comes
    from a host whose firewall let the attempt through to a port nobody
    serves -- its proxy restarting, under the very push the alert asks
    for, or down. Nor is an address the network cannot reach. What
    follows once connected -- a status line, a TLS refusal -- is
    reported, not judged: an origin that let the connection in is open.
    The certificate is not checked; whose it is changes nothing here.

    :param str ip: the address to connect to: the host's, or a name,
        resolved by the DNS -- through the CDN, for the control
    :param int port: the port to connect to (443, 80)
    :param str name: the name to ask for
    :param bool tls: speak TLS before HTTP
    :param float timeout: seconds for the connection, then for the answer
    :return: ``""`` when the connection was dropped (the wait ran out);
        ``None`` when it was refused or failed otherwise, which reads
        neither way; otherwise the status line, or what went wrong after
        connecting
    :rtype: str | None
    """
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except TimeoutError:
        return ""
    except OSError as exc:
        _logger.info(
            "origin check: %s:%s gave no reading (%s: %s)",
            ip, port, type(exc).__name__, exc,
        )
        return None
    try:
        if tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(sock, server_hostname=name)
        sock.sendall(
            f"HEAD / HTTP/1.1\r\nHost: {name}\r\n"
            f"User-Agent: incubacloud-origin-check\r\n"
            f"Connection: close\r\n\r\n".encode(),
        )
        head = sock.recv(256).split(b"\r\n", 1)[0]
        return head.decode("latin-1").strip() or "connected, no answer"
    except OSError as exc:
        return f"connected, then {type(exc).__name__}"
    finally:
        sock.close()


class CloudHost(models.Model):
    _inherit = "cloud.host"

    @api.model
    def _cron_check_posture(self):
        """Alert where a host is not as hardened, or as closed, as said.

        Hourly. The hardening question first: it reads records only, and
        the probe that follows may wait on hosts that drop it.
        """
        self._check_hardening_current()
        self._check_origin_closed()

    # ── Is its hardening the one wanted now? ────────────────────────────

    @api.model
    def _check_hardening_current(self):
        """Raise or clear ``host_hardening_outdated`` on every hardened host.

        A host never hardened from the panel is not judged: what its
        firewall holds is not the panel's to know, and the origin check
        below still asks it.
        """
        Alert = self.env["cloud.alert"]
        recipe = executor_registry.get("host_hardening")._recipe_fingerprint()
        hosts = self.sudo().search([("hardened", "=", True)])
        self._resolve_out_of_scope(HARDENING_OUTDATED_ALERT_CODE, hosts)
        for host in hosts:
            lag = host._hardening_lag(recipe)
            if lag:
                Alert.raise_alert(
                    HARDENING_OUTDATED_ALERT_CODE, lag,
                    level="warning", host=host,
                )
            else:
                Alert.resolve_alert(HARDENING_OUTDATED_ALERT_CODE, host=host)

    def _hardening_admits_only_proxies(self):
        """Whether a hardening run now would admit only the proxies on 80/443.

        Behind a CDN, refusing direct traffic, and with an IPv4 range to
        admit: an allowlist of IPv6 ranges alone would shut every IPv4
        visitor out. The one place that decides it: a hardening run
        renders the CDN allowlist on it
        (``HostHardeningExecutor._http_allowlist_vars``), and
        ``_hardening_lag`` compares it with what the last run applied.

        :rtype: bool
        """
        self.ensure_one()
        if not (self.behind_cdn and self.block_direct_access):
            return False
        return any(":" not in r for r in self._effective_trusted_proxy_ranges())

    def _hardening_lag(self, recipe):
        """Return why this host's hardening is behind what is wanted now.

        The posture first: a firewall that disagrees with the setting is
        the host answering, or refusing, everybody. Then the recipe.

        :param str recipe: fingerprint of the recipe this panel ships
        :return: the alert's message, or ``""`` when it is current
        :rtype: str
        """
        self.ensure_one()
        if not self.hardening_recipe:
            return _(
                "Host %(host)s was hardened before the panel recorded which "
                "hardening it applied, so what its firewall and security "
                "settings hold now is unknown, and the changes made to the "
                "recipe since may be missing. Whoever runs the hosts can "
                "bring it up to date from the host page with Re-apply "
                "Hardening.",
                host=self.name,
            )
        wanted = self._hardening_admits_only_proxies()
        if wanted and not self.hardening_cdn_only:
            return _(
                "Host %(host)s is set to accept only traffic through its "
                "trusted proxies, but its firewall was last hardened to let "
                "anybody in on ports 80 and 443, so its address bypasses "
                "the CDN. Whoever runs the hosts can close it from the host "
                "page with Re-apply Hardening.",
                host=self.name,
            )
        if self.hardening_cdn_only and not wanted:
            return _(
                "Host %(host)s no longer accepts only traffic through its "
                "trusted proxies, but its firewall still lets only them in "
                "on ports 80 and 443, so visitors reaching it directly get "
                "no answer. Whoever runs the hosts can open it from the "
                "host page with Re-apply Hardening.",
                host=self.name,
            )
        if self.hardening_recipe != recipe:
            return _(
                "Host %(host)s was hardened with an older recipe "
                "(%(applied)s) than the one this panel ships (%(current)s), "
                "so the changes made since are not on it. Whoever runs the "
                "hosts can bring it up to date from the host page with "
                "Re-apply Hardening.",
                host=self.name, applied=self.hardening_recipe, current=recipe,
            )
        return ""

    # ── Does a host said to be closed drop a direct connection? ─────────

    @api.model
    def _check_origin_closed(self):
        """Raise or clear ``host_origin_open`` on every host said to be closed.

        Asked of every host behind a CDN that refuses direct traffic and
        whose proxy is up -- before that nothing listens, and nothing
        claims anything. A host behind the CDN but still open is not
        asked: that is a stage it goes through, not a claim. A host that
        gave no reading this run (see :meth:`_probe_origin`) is left as
        it was: neither raised nor cleared, its first sighting kept.
        """
        Alert = self.env["cloud.alert"]
        hosts = self.sudo().search([
            ("behind_cdn", "=", True),
            ("block_direct_access", "=", True),
            ("traefik_deployed", "=", True),
            ("ip_address", "!=", _UNASSIGNED_IP),
        ])
        self._resolve_out_of_scope(ORIGIN_OPEN_ALERT_CODE, hosts)
        for host in hosts:
            seen = host._probe_origin()
            if not seen:
                Alert.resolve_alert(ORIGIN_OPEN_ALERT_CODE, host=host)
                continue
            port, name, answer = seen
            if answer is None:
                continue
            Alert.raise_alert(
                ORIGIN_OPEN_ALERT_CODE,
                _(
                    "Host %(host)s answers connections made straight to its "
                    "address, although it is set to accept only traffic "
                    "through its trusted proxies: asked for %(name)s at "
                    "%(ip)s on port %(port)s, it answered \"%(answer)s\". "
                    "Whoever runs the hosts can close it from the host page: "
                    "check that the proxy ranges are applied to it, then "
                    "Re-apply Hardening, which lets only those ranges in on "
                    "ports 80 and 443, and Push Trusted Proxy Settings.",
                    host=host.name, name=name, ip=host.ip_address,
                    port=port, answer=answer,
                ),
                level="warning", host=host,
            )

    def _probe_origin(self):
        """Connect straight to this host for a name it serves, 443 then 80.

        Dropped on both, the same name is asked through the CDN, on 443
        as a visitor would reach it: the control that tells a closed
        origin from a panel that reaches nothing directly, made per host
        and on the very path a visitor takes. Answered there, the origin
        is closed; not, or with no name to ask for, there is no reading.

        :return: ``(port, name, answer)`` for the first port that let the
            connection in; ``None`` for the closed reading; else
            ``(port, name, None)``: no reading, for the first port that
            gave none (see :func:`_origin_answer`), or for 443 when the
            name could not be reached through the CDN
        :rtype: tuple | None
        """
        self.ensure_one()
        name = self._origin_probe_name()
        no_reading = None
        for port, tls in ((443, True), (80, False)):
            answer = _origin_answer(self.ip_address, port, name, tls)
            if answer:
                return port, name, answer
            if answer is None:
                no_reading = no_reading or (port, name, None)
        if no_reading:
            return no_reading
        if name == self.ip_address or not _origin_answer(name, 443, name, True):
            return 443, name, None
        return None

    def _origin_probe_name(self):
        """Return a name this host serves, to ask for as a visitor would.

        The primary domain of its oldest instance; its own address when
        it serves none, which still tells whether the firewall lets a
        connection in, though a drop then reads neither way: there is
        no name to reach it by through the CDN.

        :rtype: str
        """
        self.ensure_one()
        instance = self.env["cloud.instance"].sudo().search(
            [("host_id", "=", self.id), ("domain", "!=", False)],
            order="id", limit=1,
        )
        return instance.domain or self.ip_address

    @api.model
    def _resolve_out_of_scope(self, code, hosts):
        """Clear *code* on every host the check no longer asks about.

        One whose setting was turned off, that was archived or deleted:
        nothing will look at it again, so an alert lit about it would
        stay lit for ever. A sighting not confirmed yet is forgotten too:
        back in scope later, the host is counted from one again.

        :param str code: the alert code
        :param hosts: the ``cloud.host`` records still asked about
        """
        Alert = self.env["cloud.alert"]
        stale = Alert.sudo().search([
            ("code", "=", code),
            ("state", "=", "active"),
            ("host_id", "not in", hosts.ids),
        ])
        for host in stale.host_id:
            Alert.resolve_alert(code, host=host)
        self.env["cloud.alert.suspicion"].sudo().search([
            ("code", "=", code),
            ("host_id", "!=", False),
            ("host_id", "not in", hosts.ids),
        ]).unlink()
