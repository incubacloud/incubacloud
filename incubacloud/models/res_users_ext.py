import logging

from psycopg2 import sql as psql

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

CRON_BOT_LOGIN = "__incubacloud_cron__"
CRON_BOT_XML_ID = "incubacloud.user_incubacloud_cron"


class ResUsers(models.Model):
    _inherit = 'res.users'

    cloud_notification_level = fields.Selection(
        selection=[
            ('all', 'All events (success + failure)'),
            ('failures', 'Failures only'),
            ('none', 'None'),
        ],
        string='Cloud Job Notifications',
        default='failures',
    )

    @api.model
    def _incubacloud_ensure_cron_bot(self):
        """Create (or re-home) the cron bot user referenced by every
        ``ir.cron`` in this module.

        Called from ``security/cloud_users.xml`` so the bot exists
        before the cron records that reference it via ``user_id``.

        Going through ``create()`` in Python rather than an XML
        ``<record>`` avoids a flaky interaction with the ``account``
        module: that module adds a ``required=True`` column
        (``autopost_bills``) to ``res.partner`` via ``_inherit``, and
        the XML record-creation path sometimes fails to cascade the
        default into the auto-created partner. ``create()`` in
        Python honours the field's ``default='ask'`` cleanly.

        Idempotent: on second install we find the existing user by
        XML-id (preferred) or by login (fallback for manually
        created users) and only align groups.
        """
        IMD = self.env['ir.model.data'].sudo()
        module, name = CRON_BOT_XML_ID.split('.', 1)
        mdata = IMD.search(
            [('module', '=', module),
             ('name', '=', name),
             ('model', '=', 'res.users')],
            limit=1,
        )
        if mdata:
            user = self.browse(mdata.res_id).exists()
        else:
            user = self.sudo().search([('login', '=', CRON_BOT_LOGIN)], limit=1)

        if not user:
            # Create the partner via raw SQL so we can fill columns
            # added by modules outside our dependency graph
            # (``account.autopost_bills`` is a required column whose
            # default cannot be applied by the ORM when ``account``
            # is not in our ``depends`` — Odoo loads its python
            # fields lazily and this migration runs before they are
            # on ``_fields``).
            # The table layout is introspected so only columns that
            # exist in THIS deployment are written. We ship known
            # defaults for the few required columns common modules
            # are known to contribute; everything else falls back
            # to ORM write after the row exists.
            known_defaults = {
                'autopost_bills': 'ask',   # account module
            }
            self.env.cr.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'res_partner'
                """,
            )
            partner_cols = {row[0] for row in self.env.cr.fetchall()}

            # Build the column / value lists as Python objects, then
            # use psycopg2.sql.Identifier to safely interpolate the
            # column names and %s placeholders for the values. This
            # is the canonical way to build dynamic-column INSERTs
            # in psycopg2 — it cleanly separates SQL identifiers
            # (quoted with double-quotes) from values (parameterised),
            # so future additions to ``known_defaults`` can never
            # introduce SQL injection even if the value comes from
            # a less-trusted source.
            now = fields.Datetime.now()
            cols = ['name', 'active', 'is_company', 'type',
                    'create_date', 'write_date',
                    'create_uid', 'write_uid']
            vals = ['IncubaCloud Cron Bot', True, False, 'contact',
                    now, now, 1, 1]
            for col, default in known_defaults.items():
                if col in partner_cols:
                    cols.append(col)
                    vals.append(default)

            query = psql.SQL(
                "INSERT INTO res_partner ({cols}) "
                "VALUES ({placeholders}) RETURNING id"
            ).format(
                cols=psql.SQL(', ').join(map(psql.Identifier, cols)),
                placeholders=psql.SQL(', ').join(
                    [psql.Placeholder()] * len(vals)
                ),
            )
            self.env.cr.execute(query, vals)
            partner_id = self.env.cr.fetchone()[0]
            self.env['res.partner'].invalidate_model()
            user = self.sudo().create({
                'login': CRON_BOT_LOGIN,
                'partner_id': partner_id,
                'active': True,
                'share': False,
                'notification_type': 'email',
            })
            _logger.info("created incubacloud cron bot user id=%s", user.id)

        # Ensure an ``ir.model.data`` row exists so cron XMLs can
        # reference the user by its fully-qualified XML-id regardless
        # of how it got created above (existing-login fallback or
        # fresh create).
        if not mdata:
            IMD.create({
                'module': module,
                'name': name,
                'model': 'res.users',
                'res_id': user.id,
                'noupdate': True,
            })

        # Align groups. Re-running is safe (``(4, id)`` is a no-op
        # when the membership already exists). We intentionally do
        # NOT remove groups an operator may have added — only add
        # the ones we need.
        required = [
            self.env.ref('incubacloud.group_cloud_manager').id,
            self.env.ref('base.group_user').id,
            self.env.ref('queue_job.group_queue_job_manager').id,
        ]
        user.sudo().write({'group_ids': [(4, gid) for gid in required]})

    @api.model
    def _incubacloud_assign_cron_user_id(self, module_name):
        """Point every ``ir.cron`` owned by *module_name* at the
        cron bot.

        Called from the post-init hook of every module that ships
        crons (this module and any module that inherits from it). We
        detect the crons by their ``ir.model.data`` rows so only rows
        the calling module actually owns are touched; any cron the
        operator created manually from the UI is left alone.

        We only overwrite ``user_id`` when it still points at uid=1
        (the OdooBot default). If an operator has re-routed a cron
        to a specific user, their choice wins.
        """
        IMD = self.env['ir.model.data'].sudo()
        bot = self.env.ref(CRON_BOT_XML_ID)
        cron_mdata = IMD.search([
            ('module', '=', module_name),
            ('model', '=', 'ir.cron'),
        ])
        if not cron_mdata:
            return
        stale = self.env['ir.cron'].sudo().browse(
            cron_mdata.mapped('res_id'),
        ).exists().filtered(lambda c: c.user_id.id == 1)
        if stale:
            stale.write({'user_id': bot.id})
            _logger.info(
                "reassigned %d %s crons to cron bot user",
                len(stale), module_name,
            )
