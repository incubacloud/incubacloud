"""Post-migrate for 1.0.20 — copier pin, drift backfill, cron to bot.

**Copier pin.** ``copier_template_ref`` gains a pinned default
(v9.6.1, sandbox-validated against the executor's answers — see
RB-15). Existing databases carry an empty value, which means "track
upstream": pin them to the same validated tag so the next tenant
deploy cannot silently pick up a new template revision.

**Config-drift backfill.** ``applied_config_hash`` (new on
cloud.instance and cloud.host) anchors the "saved vs applied" banner.
Assume the current saved state IS applied for records that completed a
deploy/setup before this mechanism existed — otherwise every existing
record would greet the operator with a false "changes not deployed".
Never-deployed records keep an empty hash (not dirty by definition).

**Cron.** 1.0.20 ships a new cron (terminal-job purge); re-run the
idempotent bot-assign helper (pattern of 1.0.15/1.0.16/1.0.19).
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    settings = env['cloud.settings'].sudo()._get()
    if not (settings.copier_template_ref or '').strip():
        settings.copier_template_ref = 'v9.6.1'

    instances = env['cloud.instance'].sudo().with_context(
        active_test=False,
    ).search(
        [('state', 'in', ('deployed', 'deleting'))],
    )
    for inst in instances:
        try:
            inst.applied_config_hash = inst._config_snapshot_hash()
        except Exception:
            # A snapshot that cannot render (e.g. an undecryptable
            # secret) must not block the migration; the record simply
            # starts untracked, exactly like a never-deployed one.
            _logger.warning(
                "1.0.20: could not backfill config hash for instance %s",
                inst.id, exc_info=True,
            )

    hosts = env['cloud.host'].sudo().with_context(active_test=False).search(
        [('traefik_deployed', '=', True)],
    )
    for host in hosts:
        try:
            host.applied_config_hash = host._config_snapshot_hash()
        except Exception:
            _logger.warning(
                "1.0.20: could not backfill config hash for host %s",
                host.id, exc_info=True,
            )

    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
