"""Settings of the direct path (plan-trafico-interno-directo).

The DNS token the direct certificates are asked for with, the central's
direct name and the state of its probe. The state moves by the rules of
``_direct_probe``; it is written on a cursor of its own, because the job
and (in the SaaS build) a probe every three minutes both write it.

The time of the last probe is the exception: it moves at every probe, so
it is not a column of the settings row but a system parameter, and the
Settings screen shows it as a read-only computed field. A probe every few
minutes then never competes with a save of the Settings screen for the one
row. The time of the last good PromQL read of the central
(``metrics_read_ok_at``) is kept the same way, for the same reason: it
moves at every pass of the specs cron.
"""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import _direct_probe
from ._concurrency import read_committed_cursor
from .cloud_instance_domain import _HOSTNAME_RE
from .encrypted_char import EncryptedChar

#: How recent a good PromQL read must be for the central to count as up.
_READ_FRESH = timedelta(minutes=10)

#: Where the time of a name's last probe is kept; ``%s`` is its prefix.
_CHECKED_AT_KEY = "incubacloud.direct_checked_at.%s"

#: The "prefix" under which the time of the last good read of the central
#: is kept, beside the names' own (so a copy forgets them all together).
_READ_OK_PREFIX = "metrics_read_ok"


class CloudSettings(models.Model):
    _inherit = "cloud.settings"

    acme_dns_cloudflare_token = EncryptedChar(
        string="DNS token for direct certificates (Cloudflare)",
        groups="incubacloud.group_cloud_manager",
        help="Cloudflare API token that may only read the zone and edit its "
             "DNS. Traefik asks Let's Encrypt for the direct names' "
             "certificates with it (DNS-01). Written to each host that needs "
             "it as a root-only file, never in its compose file. Stored "
             "encrypted.",
    )
    metrics_central_direct_host = fields.Char(
        string="Central's direct name",
        help="Name under which the platform's own servers reach the central "
             "on its host's direct port, without the CDN, e.g. "
             "direct.metrics.example.com: two levels under the zone, DNS "
             "only, no AAAA. Empty: every agent writes to the remote-write "
             "URL.",
    )
    metrics_central_direct_state = fields.Selection(
        _direct_probe.STATES, string="Central's direct name state",
        default="unknown", readonly=True, copy=False,
    )
    metrics_central_direct_failures = fields.Integer(readonly=True, copy=False)
    metrics_central_direct_successes = fields.Integer(readonly=True, copy=False)
    metrics_central_direct_checked_at = fields.Datetime(
        string="Central's direct name last probed",
        compute="_compute_metrics_central_direct_checked_at",
        help="When the name was last probed. Kept in a system parameter, not "
             "in this row: it moves at every probe.",
    )
    metrics_central_direct_failing_since = fields.Datetime(
        readonly=True, copy=False,
    )
    metrics_central_direct_dns_bad = fields.Boolean(readonly=True, copy=False)
    metrics_central_direct_cert_expires = fields.Datetime(
        readonly=True, copy=False,
    )
    metrics_central_direct_error = fields.Char(readonly=True, copy=False)
    metrics_read_ok_at = fields.Datetime(
        string="Metrics last read",
        compute="_compute_metrics_read_ok_at",
        inverse="_inverse_metrics_read_ok_at",
        help="Last time a PromQL read of the central answered. Says the "
             "central is up, not that its direct port is. Kept in a system "
             "parameter, not in this row: it moves at every pass.",
    )

    def _compute_metrics_read_ok_at(self):
        """Show when the central last answered a PromQL read."""
        read = self._direct_checked_at(_READ_OK_PREFIX)
        for settings in self:
            settings.metrics_read_ok_at = read

    def _inverse_metrics_read_ok_at(self):
        """Keep the time assigned to ``metrics_read_ok_at`` (False forgets it)."""
        for settings in self:
            self._direct_note_checked(_READ_OK_PREFIX, settings.metrics_read_ok_at)

    def _compute_metrics_central_direct_checked_at(self):
        """Show when the central's direct name was last probed."""
        checked = self._direct_checked_at("metrics_central_direct")
        for settings in self:
            settings.metrics_central_direct_checked_at = checked

    @api.model
    def _direct_checked_at(self, prefix):
        """Return when the direct name *prefix* was last probed.

        Read with SQL, as :meth:`_direct_note_checked` writes it: the
        cached ``get_param`` would not see those writes. Nothing else
        reads these keys.

        :param str prefix: ``metrics_central_direct`` (or
            ``saas_backchannel`` in the SaaS build).
        :return: a naive UTC datetime, or False before the first probe.
        :rtype: datetime | bool
        """
        self.env["ir.config_parameter"].flush_model(["key", "value"])
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter WHERE key = %s",
            [_CHECKED_AT_KEY % prefix],
        )
        row = self.env.cr.fetchone()
        return fields.Datetime.to_datetime(row[0]) if row and row[0] else False

    @api.model
    def _direct_note_checked(self, prefix, when):
        """Record when the direct name *prefix* was probed; False forgets it.

        With SQL, not ``set_param``: every write through the model empties
        the registry's ``stable`` caches on every worker (access rules,
        record rules, templates), and this runs at every probe and at
        every pass of the specs cron. The row is still a system parameter,
        so a neutralized copy forgets it with the others.

        :param str prefix: as in :meth:`_direct_checked_at`.
        :param when: a datetime, or False for a name that starts over.
        """
        params = self.env["ir.config_parameter"]
        params.flush_model(["key", "value"])
        key = _CHECKED_AT_KEY % prefix
        if when:
            self.env.cr.execute(
                """
                INSERT INTO ir_config_parameter
                    (key, value, create_uid, create_date, write_uid, write_date)
                VALUES (%s, %s, %s, now() at time zone 'UTC',
                        %s, now() at time zone 'UTC')
                ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value,
                       write_uid = EXCLUDED.write_uid,
                       write_date = EXCLUDED.write_date
                """,
                [key, fields.Datetime.to_string(when), self.env.uid, self.env.uid],
            )
        else:
            self.env.cr.execute(
                "DELETE FROM ir_config_parameter WHERE key = %s", [key],
            )
        params.invalidate_model(["key", "value"])

    @api.constrains("metrics_central_direct_host")
    def _check_metrics_central_direct_host(self):
        """Refuse a central's direct name that cannot be one.

        :raise ValidationError: saying why.
        """
        for settings in self:
            problem = settings._direct_name_problem(
                settings.metrics_central_direct_host,
            )
            if problem:
                raise ValidationError(problem)

    def _direct_name_problem(self, name):
        """Return why *name* cannot be a direct name, or ''.

        A host name, and one the default certificate of no host with a
        direct port covers: the ACME prune would retire its certificate,
        and the proxy would serve the default one for it.

        :param str name: the name typed.
        :rtype: str
        """
        if not name:
            return ""
        if "." not in name or not _HOSTNAME_RE.fullmatch(name):
            return _(
                "%(name)s is not a host name. Write the name alone, for "
                "example metrics.example.com: no https:// and no path.",
                name=name,
            )
        hosts = self.env["cloud.host"].sudo().search([
            ("active", "=", True), ("direct_port", ">", 0),
        ])
        if any(host._tls_default_covers(name) for host in hosts):
            return _(
                "%(name)s is covered by the host's default certificate; use a "
                "two-level name, for example direct.metrics.example.com",
                name=name,
            )
        return ""

    def _direct_central_base(self):
        """Return ``https://<central's direct name>:<port>`` while it works.

        Empty unless the name is set, the central's host has a direct port
        applied and the probe says ``ok``.

        :rtype: str
        """
        settings = self._get_system()
        name = (settings.metrics_central_direct_host or "").strip().lower()
        host = settings.metrics_central_host_id
        if not (
            name and host and host.direct_port_shipped
            and settings.metrics_central_direct_state == "ok"
        ):
            return ""
        return f"https://{name}:{host.direct_port_shipped}"

    def _direct_central_write_url(self):
        """Return the central's direct remote-write URL while it works, or ''.

        :rtype: str
        """
        base = self._direct_central_base()
        return f"{base}/w/api/v1/write" if base else ""

    def _direct_path_in_force(self):
        """Whether this panel's hosts may be offered the central's direct name.

        Core: its own name works. A tenant's panel has no name of its own
        and answers from what its manager sends it.

        :rtype: bool
        """
        return bool(self._direct_central_base())

    def _direct_name_in_force(self, prefix):
        """Whether the direct name *prefix* works, as this panel knows it.

        A host's own try of a name in force that fails is the host's
        fault (``cloud.host._record_direct_reach``). Core: the name's
        probe says ``ok``. A layer whose panel does not probe the name
        (a tenant's, offered the central's name by its manager) answers
        from what it is sent instead.

        :param str prefix: ``metrics_central_direct`` (or
            ``saas_backchannel`` in the SaaS build).
        :rtype: bool
        """
        return self._get_system()[f"{prefix}_state"] == "ok"

    def _direct_central_healthy(self):
        """Whether the central answers and its direct path is in force.

        The read proves the central responds (on the master it goes by the
        docker bridge, not the direct port); the direct port is the
        probe's to say. Together they keep a central outage from
        re-applying the whole fleet.

        :rtype: bool
        """
        read = self._get_system().metrics_read_ok_at
        return bool(
            self._direct_path_in_force()
            and read and read >= fields.Datetime.now() - _READ_FRESH
        )

    @api.model
    def _direct_note_read_ok(self):
        """Stamp a PromQL read of the central that answered.

        A system parameter, like the time of a probe: the specs cron
        writes it at every pass and must not compete with a save of the
        Settings screen for the one row.
        """
        self._direct_note_checked(_READ_OK_PREFIX, fields.Datetime.now())

    @api.model
    def _direct_probe_apply(self, prefix, outcome):
        """Apply one probe *outcome* to the direct name *prefix*.

        On a READ COMMITTED cursor of its own, with the settings row locked
        before it is read: the job and the light probe write the same row
        and must wait for each other, not lose to each other; the second
        reads what the first committed. What did not move is not written,
        and in a steady state that is all of the settings row: only the
        time of the probe moves, in its system parameter.

        :param str prefix: ``metrics_central_direct`` (or
            ``saas_backchannel`` in the SaaS build).
        :param dict outcome: as ``_direct_probe.parse_probe`` returns it.
        :return: ``(previous state, new state)``.
        :rtype: tuple
        """
        now = fields.Datetime.now()
        with read_committed_cursor(self.env.registry) as cr:
            env = self.env(cr=cr)
            settings = env["cloud.settings"].sudo()._get_system()
            cr.execute(
                "SELECT id FROM cloud_settings WHERE id = %s FOR UPDATE",
                [settings.id],
            )
            settings.invalidate_recordset()
            current = {s: settings[f"{prefix}_{s}"] for s in _direct_probe.SUFFIXES}
            new = _direct_probe.advance(current, outcome, now)
            vals = {f"{prefix}_{s}": v for s, v in new.items() if v != current[s]}
            cert = outcome.get("cert_expires")
            if cert and cert != settings[f"{prefix}_cert_expires"]:
                vals[f"{prefix}_cert_expires"] = cert
            if not outcome["ok"]:
                error = (
                    f"{outcome['layer']}: {outcome.get('detail') or ''}"
                ).strip()[:200]
                if error != (settings[f"{prefix}_error"] or ""):
                    vals[f"{prefix}_error"] = error
            if vals:
                settings.write(vals)
            settings._direct_note_checked(prefix, now)
            settings._direct_alerts(prefix, current["state"], new["state"], outcome)
        return current["state"], new["state"]

    def _direct_alerts(self, prefix, old_state, new_state, outcome):
        """Raise or close the alerts of the direct name *prefix*.

        One per name (the name is the alert's key): ``direct_path_down``
        on entering ``failing``, closed on ``ok``; a warning while its
        certificate has less than 21 days left.
        """
        name = self[f"{prefix}_host"] or prefix
        Alert = self.env["cloud.alert"].sudo()
        if new_state == "failing" and old_state != "failing":
            Alert.raise_alert(
                "direct_path_down",
                _(
                    "The direct path %(name)s does not answer (%(layer)s). "
                    "Hosts and panels are back on the public path.",
                    name=name, layer=outcome.get("layer") or "?",
                ),
                level="critical", key=name,
            )
        elif new_state == "ok" and old_state != "ok":
            Alert.resolve_alert("direct_path_down", key=name)
        cert = self[f"{prefix}_cert_expires"]
        if cert and cert - fields.Datetime.now() < _direct_probe.CERT_WARNING:
            Alert.raise_alert(
                "direct_certificate_expiring",
                _(
                    "The certificate of the direct path %(name)s expires on "
                    "%(date)s.",
                    name=name, date=fields.Date.to_string(cert.date()),
                ),
                key=name,
            )
        elif cert:
            Alert.resolve_alert("direct_certificate_expiring", key=name)

    def _direct_forget_alerts(self, name):
        """Close the alerts of a direct name that is no longer used."""
        Alert = self.env["cloud.alert"].sudo()
        for code in ("direct_path_down", "direct_certificate_expiring"):
            Alert.resolve_alert(code, key=name)

    def write(self, vals):
        """Start the central's direct name over when it changes; converge.

        A new name has proved nothing yet: ``unknown``, counters at 0,
        the old name's alerts closed and the time of its last probe
        forgotten. A change of name or of state re-applies, as soon as
        this commits, whatever depends on it.
        """
        renamed = "metrics_central_direct_host" in vals and any(
            (rec.metrics_central_direct_host or "")
            != (vals["metrics_central_direct_host"] or "")
            for rec in self
        )
        old_names = (
            {rec.metrics_central_direct_host for rec in self} if renamed else set()
        )
        old_states = (
            {rec.id: rec.metrics_central_direct_state for rec in self}
            if "metrics_central_direct_state" in vals else {}
        )
        if renamed:
            vals = {**vals, **_direct_probe.reset_values("metrics_central_direct")}
        result = super().write(vals)
        if renamed:
            self._direct_note_checked("metrics_central_direct", False)
        for name in filter(None, old_names):
            self._direct_forget_alerts(name)
        if renamed:
            host = self._get_system().metrics_central_host_id
            if host and host.direct_port_shipped:
                host._enqueue_direct_sync("central's direct name changed")
        moved = renamed or any(
            rec.metrics_central_direct_state != old_states[rec.id]
            for rec in self if rec.id in old_states
        )
        if moved:
            self.env["cloud.host"]._direct_path_changed("central's direct name")
        return result
