import logging
import os

from psycopg2 import sql

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

from .encrypted_char import EncryptedChar
from .password_utils import key_is_configured, rotate_value

_logger = logging.getLogger(__name__)


class CloudSettings(models.Model):
    """System-wide IncubaCloud settings — singleton.

    Stores sensitive credentials that must not live in
    ``ir.config_parameter`` (plain text in the database).
    Fields use ``EncryptedChar`` (Fernet) so the DB column
    always holds ``enc:<token>``.
    """

    _name = "cloud.settings"
    _inherit = ["cloud.security.mixin"]
    _description = "IncubaCloud Settings"

    # ── GitHub credentials ─────────────────────────────────────────────────

    github_pat = EncryptedChar(
        string="GitHub Personal Access Token",
        groups="incubacloud.group_cloud_manager",
        help=(
            "PAT used as fallback when no GitHub App is configured. "
            "Stored encrypted — never exposed in logs or API responses."
        ),
    )

    # ── Job log retention ──────────────────────────────────────────────────
    # cloud.job.log.chunk grows monotonically (every SSH command stream
    # writes hundreds of rows per job). The daily purge cron deletes
    # chunks older than this many days that belong to terminal jobs.
    # Active jobs always keep their full log regardless of age.

    job_log_retention_days = fields.Integer(
        string='Job log retention (days)',
        default=30,
        help='Number of days to keep job log chunks (cloud.job.log.chunk) '
             'for terminated jobs. Active jobs are never purged. Set to 0 '
             'to disable purging entirely (table will grow unbounded).',
    )

    # cloud.job rows themselves also grow monotonically (the fourth leak
    # of the retention family). They feed the instance timeline in the
    # UI, so the window is much longer than the chunk one: purging a job
    # trims visible history to this many days.
    job_retention_days = fields.Integer(
        string='Job retention (days)',
        default=180,
        help='Number of days to keep terminal cloud.job rows (done, '
             'failed, cancelled). Active jobs are never purged. Purging '
             'a job also removes its remaining log chunks and trims the '
             'instance timeline to this window. Set to 0 to disable '
             'purging entirely (table will grow unbounded).',
    )

    # ── GitHub event retention ────────────────────────────────────────────
    # cloud.github.event stores the raw webhook JSON. Push payloads for
    # large monorepos are 100 KB+. Keeping them forever bloats backups
    # and keeps commit metadata (authors, messages, diff refs) around
    # beyond any useful audit window. Two-stage retention:
    #   * After ``github_event_truncate_days``: payload is replaced by a
    #     compact stub (event_type, ref, sha, action, original_size).
    #     Event row is preserved for audit; only the blob is shed.
    #   * After ``github_event_retention_days``: the row itself is
    #     unlinked. Only rows with ``processed=True`` are touched —
    #     unprocessed/error events remain until an operator investigates.

    github_event_retention_days = fields.Integer(
        string='GitHub event retention (days)',
        default=90,
        help='Number of days to keep processed cloud.github.event rows. '
             'Unprocessed/error rows are never auto-deleted. Set to 0 to '
             'disable deletion entirely.',
    )
    github_event_truncate_days = fields.Integer(
        string='GitHub event payload truncation (days)',
        default=7,
        help='After this many days, the raw JSON payload of a processed '
             'cloud.github.event is replaced by a compact stub (event '
             'type, ref, sha, action). Set to 0 to disable truncation.',
    )

    # ── Doodba template used to scaffold instances ────────────────────────
    # Every deploy runs ``copier copy`` against this template. Left
    # unpinned, each deploy silently picks up whatever is on the
    # template's default branch at that moment, so two instances created
    # a week apart can be built from different upstream revisions and a
    # breaking change upstream lands on the next tenant deploy with no
    # signal here. Pinning a tag makes template upgrades a decision.

    copier_template_url = fields.Char(
        string='Doodba template',
        default='gh:Tecnativa/doodba-copier-template',
        help='Copier source for new instances. Change it to deploy from a '
             'fork of the doodba template.',
    )
    # Default pin: v9.6.1, validated 2026-07-30 by rendering the template
    # in a sandbox with the exact answers the deploy executor emits
    # (domains/redirects in Traefik labels, backup DST, secrets excluded
    # from the answers file by the template's own secret marking). Bump
    # via the runbook (RB-15): sandbox render first, then staging canary.
    copier_template_ref = fields.Char(
        string='Doodba template version',
        default='v9.6.1',
        help='Git tag/branch/commit to pin the template to (copier '
             "--vcs-ref), e.g. 'v9.6.1'. Empty means the template's "
             'default branch: deploys then track upstream automatically '
             'and an upstream change can alter the next deploy without '
             'warning. The effective ref is recorded in the job log.',
    )

    # ── Rate limiting caps ────────────────────────────────────────────────
    # Read by ``cloud.rate.limit._get_cap`` when throttling the two
    # abuse-prone public/auth'd endpoints. All values are per-minute
    # tumbling windows. Setting any of them to 0 falls back to the
    # documented default so a misconfigured 0 can't accidentally
    # lock every request out.

    rate_limit_webhook_per_min = fields.Integer(
        string='GitHub webhook (per IP, per minute)',
        default=300,
        help='Max requests/min the ``/cloud/github/webhook`` endpoint '
             'accepts from a single client IP. GitHub can burst heavily '
             'on a push to a monorepo with many workflow triggers, so '
             'the default is generous; lower it if you see abuse. Set '
             'to 0 to fall back to the documented default (300).',
    )
    rate_limit_terminal_per_min = fields.Integer(
        string='Terminal open (per instance, per minute)',
        default=30,
        help='Max ``/cloud/terminal/open`` calls/min targeting the same '
             'cloud.instance, across all users. Protects the remote '
             'host from a multi-operator debug storm.',
    )
    rate_limit_terminal_user_per_min = fields.Integer(
        string='Terminal open (per user, per minute)',
        default=10,
        help='Max ``/cloud/terminal/open`` calls/min from a single '
             'internal user. Caps both legitimate human operators and '
             'any compromised account running a script. Each opened '
             'session holds an SSH connection + PTY buffer until '
             'SESSION_TIMEOUT.',
    )
    rate_limit_connect_per_min = fields.Integer(
        string='Connect as user (per instance, per minute)',
        default=20,
        help='Max connect-as calls/min targeting the same cloud.instance, '
             'across all users. Each call opens an SSH connection and runs '
             'a script inside the tenant container.',
    )
    rate_limit_connect_user_per_min = fields.Integer(
        string='Connect as user (per user, per minute)',
        default=10,
        help='Max connect-as calls/min from a single panel user. Caps a '
             'compromised account from enumerating tenant users or minting '
             'impersonation tokens in bulk.',
    )

    # ── Backup usage alert default ────────────────────────────────────────
    # Per-backend ``alert_threshold_pct`` can opt in (1–100), opt out
    # (-1 = disabled), or inherit (0 = use this default). This system-wide
    # default keeps the per-backend form short for the common case while
    # still letting an operator dial it per backend when needed.

    default_backup_alert_threshold_pct = fields.Integer(
        string='Default backup alert threshold (%)',
        default=80,
        help='Default percentage at which an email alert is sent when a '
             "backup backend's usage approaches its quota. Per-backend "
             "overrides win — set the backend's own threshold to 0 to "
             'inherit this value, -1 to disable, or 1–100 for an explicit '
             'value.',
    )

    # ── Observability ──────────────────────────────────────────────────────
    # Agents push metrics outbound to a central VictoriaMetrics; the panel
    # reads it back over PromQL to evaluate ``cloud.metric.rule``. All of
    # it is inert until ``metrics_enabled`` is set, so the feature can ship
    # and be deployed before anyone turns it on.

    metrics_enabled = fields.Boolean(
        string='Enable observability',
        default=False,
        help='Master switch. When off, no agent is installed, the metric '
             'rules are not evaluated and no metrics alert is raised.',
    )
    metrics_central_url = fields.Char(
        string='Metrics backend URL',
        help='Base URL the PANEL uses to query metrics (PromQL). It must '
             'resolve from inside the panel container — e.g. '
             'http://host.docker.internal:8428 for a co-located backend, '
             'or its public URL when it lives on another server.',
    )
    metrics_remote_write_url = fields.Char(
        string='Remote-write URL',
        help='Endpoint each host AGENT pushes to. It must resolve from '
             'inside the agent container on every host — so 127.0.0.1 is '
             'almost never right: that is the agent itself. Use '
             'http://host.docker.internal:8428/api/v1/write only for '
             'agents on the same server as the backend, and a public '
             'HTTPS URL for every other host. Outbound only: no inbound '
             'port is opened, which is what keeps BYOH and NAT hosts '
             'working.',
    )
    metrics_remote_write_token = EncryptedChar(
        string='Remote-write token',
        help='Shared secret protecting the central. The agents present '
             'it as HTTP basic auth when pushing, and the panel presents '
             'it when querying; the central rejects anonymous access '
             'while it is set. Stored encrypted and written to each host '
             'as root-only 0600. Changing it means redeploying the '
             'central AND re-running Install Observability everywhere — '
             'until both sides match, pushes are refused.',
    )
    metrics_retention_days = fields.Integer(
        string='Metrics retention (days)',
        default=90,
        help='How long the central keeps series. Passed to the central '
             'deployment playbook.',
    )
    grafana_base_url = fields.Char(
        string='Grafana base URL',
        help='Base URL of the Grafana embedded in the panel, e.g. '
             'https://grafana.example.com. Empty hides the Monitoring tab.',
    )

    # ── Singleton constraint ───────────────────────────────────────────────

    @api.constrains("github_pat")
    def _check_singleton(self):
        if self.env["cloud.settings"].search_count([]) > 1:
            raise UserError(
                _("Only one IncubaCloud settings record is allowed.")
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    @api.model
    def _get(self):
        """Return the settings singleton — requires manager ACL.

        The model's ``ir.model.access`` is restricted to
        ``group_cloud_manager``. Use this accessor from controllers that
        are gated to manager (or higher) so the ACL acts as a
        defense-in-depth check on top of the role gate. For trusted
        non-manager flows (cron jobs, queue_job workers, providers,
        webhooks, public endpoints, or any inheriting module that
        legitimately needs settings access without a manager gate) use
        :meth:`_get_system` instead.
        """
        rec = self.search([], limit=1)
        if not rec:
            rec = self.create({})
        return rec

    @api.model
    def _get_system(self):
        """Return the settings singleton, bypassing ACL via sudo.

        Reserved for system contexts where the caller is *not* a manager
        but legitimately needs to read settings: cron jobs, queue
        workers, provider integrations, public webhooks, controllers
        exposed to portal users that read non-secret feature flags,
        and any inheriting module operating in a system context. Never
        use from an admin-write path — those must go through
        :meth:`_get` after a manager gate.
        """
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({})
        return rec

    # ── Startup check: INCUBACLOUD_SECRET_KEY ──────────────────────────────

    def _register_hook(self):
        """Warn loudly at registry load if the encryption key is missing.

        The actual fail-loud behaviour lives in ``password_utils``: any
        attempt to encrypt/decrypt raises. This hook only surfaces the
        misconfiguration at startup so operators don't discover it at the
        first write. Suppressed during tests (the key is patched per-test)
        and behind ``INCUBACLOUD_ALLOW_NO_KEY=1`` for ad-hoc dev scripts
        that deliberately avoid the key.
        """
        super()._register_hook()
        if tools.config.get('test_enable'):
            return
        if os.environ.get('INCUBACLOUD_ALLOW_NO_KEY'):
            _logger.warning(
                "INCUBACLOUD_ALLOW_NO_KEY=1 — any write to an encrypted "
                "field will still raise at runtime."
            )
            return
        if not key_is_configured():
            _logger.critical(
                "INCUBACLOUD_SECRET_KEY is missing or invalid. Any write to "
                "cloud.host.password, cloud.instance.*_password, "
                "cloud.backup.backend.passphrase, cloud.settings.github_pat "
                "and similar fields will raise. Configure the variable in "
                "the Odoo container environment before going to production."
            )

    # ── Secret rotation ────────────────────────────────────────────────────
    # Rotation workflow (operator-driven, not time-based):
    #   1. Generate a new key, set INCUBACLOUD_SECRET_KEY="<new>,<old>".
    #   2. Restart Odoo. New writes already use the new key; old rows
    #      still decrypt via the trailing old key in the MultiFernet.
    #   3. Activate ``cron_rotate_encrypted_secrets`` (or call
    #      ``_rotate_all_secrets`` manually) to re-encrypt existing rows.
    #   4. When every field reports zero rotated rows, remove the old
    #      key from the env var and restart.
    # The cron ships with active=False — turning it on is a deliberate
    # operator action, so it can't run mid-rotation on a half-configured
    # env var and corrupt ciphertext.

    @api.model
    def _rotate_all_secrets(self, batch_size=500):
        """Re-encrypt every ``EncryptedChar`` value with the primary key.

        Reads ciphertext with raw SQL so plain text is never materialised
        in Python memory. Iterates every model in the registry, picks up
        any stored ``EncryptedChar`` field, and rotates row-by-row.
        Invalidates the ORM cache per field so subsequent reads in the
        same transaction return the freshly rotated value.

        **Tolerance**: rotation of one row failing (typically because
        the ciphertext was encrypted with a key no longer present in
        the current MultiFernet chain) MUST NOT abort the whole run.
        We catch per-row exceptions, log them, and keep going so the
        remaining rotatable values still make it to the new key. Every
        stranded row is surfaced as a ``cloud.alert`` linked to the
        owning host / instance / project (or just logged when the model
        has no such relation, e.g. ``cloud.settings``).

        Returns ``{table.field: {'rotated': n, 'failed': [id, ...]}}``.
        """
        stats = {}
        for Model in self.env.registry.values():
            # Abstract mixins (e.g. cloud.terminal.route.mixin) can carry
            # an EncryptedChar field but have no table of their own — the
            # concrete models that inherit them are rotated instead. Skip
            # them so we never query a non-existent relation.
            if Model._abstract or Model._transient:
                continue
            enc_fields = [
                name for name, f in Model._fields.items()
                if isinstance(f, EncryptedChar) and f.store
            ]
            if not enc_fields:
                continue
            table = Model._table
            for field_name in enc_fields:
                rotated, failed = self._rotate_encrypted_column(
                    table, field_name, batch_size,
                )
                if rotated or failed:
                    stats[f"{table}.{field_name}"] = {
                        'rotated': rotated, 'failed': failed,
                    }
                if rotated:
                    self.env[Model._name].invalidate_model([field_name])
                if failed:
                    self._raise_rotation_alerts(
                        Model._name, field_name, failed,
                    )
        if stats:
            _logger.info("Rotated encrypted secrets: %s", stats)
        else:
            _logger.info("Rotate pass complete: nothing to rotate.")
        return stats

    def _rotate_encrypted_column(self, table, field_name, batch_size):
        """Rotate every ``enc:`` value in *table.field_name*.

        ``psycopg2.sql.Identifier`` keeps identifiers safe even though
        they come from model metadata (never user input) — belt-and-
        braces in case a model developer ever picks a weird column name.

        Returns ``(rotated_count, failed_ids)``. A row lands in
        ``failed_ids`` when ``rotate_value`` raises — typically because
        its ciphertext was encrypted with a key no longer in the
        ``INCUBACLOUD_SECRET_KEY`` chain. We swallow the exception,
        log it, and move on so one stranded value never blocks the
        rotation of the rest.
        """
        cr = self.env.cr
        cr.execute(
            sql.SQL(
                "SELECT id, {f} FROM {t} "
                "WHERE {f} LIKE 'enc:%%' ORDER BY id"
            ).format(
                t=sql.Identifier(table),
                f=sql.Identifier(field_name),
            )
        )
        rows = cr.fetchall()
        rotated = 0
        failed = []
        update_stmt = sql.SQL(
            "UPDATE {t} SET {f} = %s WHERE id = %s"
        ).format(
            t=sql.Identifier(table),
            f=sql.Identifier(field_name),
        )
        for start in range(0, len(rows), batch_size):
            for rid, old_value in rows[start:start + batch_size]:
                try:
                    new_value = rotate_value(old_value)
                except Exception as e:  # noqa: BLE001
                    failed.append(rid)
                    _logger.warning(
                        "[rotate] %s.%s id=%s could not rotate: %s "
                        "(likely encrypted with a retired key; "
                        "regenerate this value manually)",
                        table, field_name, rid, e,
                    )
                    continue
                if new_value != old_value:
                    cr.execute(update_stmt, (new_value, rid))
                    rotated += 1
        return rotated, failed

    # Models that ARE the alert target — an alert on a host points to
    # the host itself via ``host_id``.
    _ROTATION_SELF_ALERT = {
        'cloud.host': 'host_id',
        'cloud.instance': 'instance_id',
        'cloud.project': 'project_id',
    }

    def _rotation_alert_link(self, Model, record):
        """Return ``(link_field, link_id)`` pointing to the cloud.alert
        target for *record*, or ``(None, None)`` when the model has no
        host/instance/project relation and can't carry an alert.
        """
        self_target = self._ROTATION_SELF_ALERT.get(Model._name)
        if self_target:
            return self_target, record.id
        for candidate in ('host_id', 'instance_id', 'project_id'):
            if candidate in Model._fields:
                ref = record[candidate]
                if ref:
                    return candidate, ref.id
        return None, None

    def _raise_rotation_alerts(self, model_name, field_name, failed_ids):
        """Open a ``cloud.alert`` per stranded row so operators see the
        list in the dashboard without digging through logs.

        When the model is not linked to a host/instance/project (e.g.
        ``cloud.settings``, ``cloud.github.app``) we skip the alert —
        a targetless row would be invisible to the member-scoped record
        rule and impossible to trace back to a record. Those cases stay
        log-only, which is acceptable because the same warning was
        already emitted in ``_rotate_encrypted_column``.
        """
        Alert = self.env['cloud.alert'].sudo()
        Model = self.env[model_name]
        code = f"secret_rotation_stranded:{model_name}.{field_name}"
        log_only = []

        for rid in failed_ids:
            record = Model.sudo().browse(rid)
            if not record.exists():
                continue
            link_field, link_id = self._rotation_alert_link(Model, record)
            if not link_field:
                log_only.append(rid)
                continue
            # Dedup: one active alert per (code, target) at a time.
            if Alert.search_count([
                ('code', '=', code),
                ('state', '=', 'active'),
                (link_field, '=', link_id),
            ], limit=1):
                continue
            Alert.create({
                'code': code,
                'level': 'warning',
                link_field: link_id,
                'message': (
                    f"Could not rotate {model_name}.{field_name} "
                    f"(id={rid}) — ciphertext was encrypted with a key "
                    f"no longer in INCUBACLOUD_SECRET_KEY. Regenerate "
                    f"the value manually to resolve."
                ),
            })

        if log_only:
            _logger.warning(
                "[rotate] %s.%s has %d stranded row(s) with no "
                "host/instance/project to link an alert to: %s",
                model_name, field_name, len(log_only), log_only,
            )
