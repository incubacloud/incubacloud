import logging
from datetime import timedelta

import psycopg2

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import mute_logger

from ..mail_layout import wrap_in_mail_layout
from .encrypted_char import EncryptedChar

_logger = logging.getLogger(__name__)

CRON_BOT_LOGIN = "__incubacloud_cron__"
CRON_BOT_XML_ID = "incubacloud.user_incubacloud_cron"


def as_platform(records):
    """Re-bind *records* so the platform, not the caller, is the author.

    Odoo's ``sudo()`` raises privileges but leaves ``env.uid`` alone, so
    work the platform does on its own account inside somebody's request
    — preparing an instance during a signup, rebuilding the fleet from
    a GitHub push, filing an alert — is stamped with whoever happened to
    trigger it. That author then outlives the request: it lands on the
    record, on ``queue_job.user_id`` (so the executor *runs* as them),
    and on every row that executor writes. Customers have shown up as
    the authors of machines they never saw, and the public user as the
    author of 348 fleet rebuilds.

    Elevate once at the entry point and the whole chain below inherits
    the bot; there is no need to wrap individual ``create`` calls.

    ``sudo()`` comes after ``with_user`` deliberately: ``with_user``
    resets ``su``, and every caller here already held it. This changes
    *identity*, never privilege.

    Degrades to the caller's own environment when the bot cannot be
    resolved — a missing bot should cost a slightly wrong ``create_uid``,
    not the operation.

    :param records: any recordset (its ``env`` is what gets rebound).
    :return: the same recordset bound to the platform bot.
    """
    bot = records.env['res.users']._get_cron_bot()
    if not bot:
        return records
    return records.with_user(bot).sudo()


def telegram_markdown(value, bold=False):
    """Return *value* as text in a Telegram message sent as ``Markdown``.

    The notifications of jobs and alerts go to the Bot API with
    ``parse_mode: Markdown`` (the legacy one), and they carry names and
    messages that whoever may write the record wrote: ``[x](url)`` in an
    alert's message, or in the name of the job that resolved it, was a
    live link in everyone's chat, and a lone ``_`` or
    ``*`` made Telegram refuse the whole message. Outside an entity the
    four characters that open one (``_ * ` [``) are escaped with a
    backslash. Inside one Telegram reads everything as it is up to the
    character that closes it and knows no escape, so in bold only ``*``
    matters: the bold is closed before it and opened again after it.

    :param value: the text, or nothing
    :param bool bold: whether to return it as a bold entity
    :return: Markdown that shows *value* as it is
    :rtype: str
    """
    text = str(value or '')
    if bold:
        return '*' + text.replace('*', '*\\**') + '*'
    return ''.join('\\' + char if char in '_*`[' else char for char in text)


class ResUsers(models.Model):
    _inherit = 'res.users'

    cloud_notification_level = fields.Selection(
        selection=[
            ('all', 'All events (success + failure)'),
            ('failures_warnings', 'Failures and warnings'),
            ('failures', 'Failures only'),
            ('none', 'None'),
        ],
        string='Cloud Notifications',
        # Warnings reach a new user too: on the default before 1.0.193,
        # a failed backup archive or a declined payment reached nobody.
        default='failures_warnings',
        help='What reaches this user, for jobs and alerts alike. All: '
             'every job outcome and every alert. Failures and warnings: '
             'failed jobs, and critical and warning alerts. Failures '
             'only: failed jobs and critical alerts. None: nothing.',
    )

    cloud_email_enabled = fields.Boolean(
        string='Email Notifications Enabled',
        default=True,
        help='When unchecked, no cloud emails are sent to this user '
             'regardless of their notification level. Other '
             'notification channels are not affected.',
    )

    cloud_notification_mode = fields.Selection(
        selection=[
            ('immediate', 'Immediate'),
            ('daily_digest', 'Daily digest'),
        ],
        string='Cloud Email Delivery',
        default='immediate',
        help=(
            'Immediate: one email per event. Daily digest: events are '
            'batched into one summary email per day — except severe '
            'failures (production deploys/rebuilds, critical alerts), '
            'which are always sent immediately.'
        ),
    )

    cloud_muted_project_ids = fields.Many2many(
        comodel_name='cloud.project',
        relation='cloud_user_muted_project_rel',
        column1='user_id',
        column2='project_id',
        string='Muted Cloud Projects',
        help=(
            'Projects that never interrupt this user: no emails, no '
            'digest entries, no live toasts. The alerts panel and '
            'badge still show everything.'
        ),
    )

    cloud_last_digest_at = fields.Datetime(
        string='Last Cloud Digest Sent',
        help=(
            'Watermark of the daily digest window. Advanced after each '
            'run — also when the window turned out empty.'
        ),
    )

    # Closing the first steps card is a choice of whoever closes it, not
    # of the panel (``cloud.first.steps._state``).
    cloud_first_steps_display = fields.Selection(
        selection=[
            ('auto', 'While Steps Are Left'),
            ('hidden', 'Closed'),
            ('shown', 'Opened Again'),
        ],
        string='First Steps Card',
        default='auto',
        help=(
            'Whether the first steps card shows on the home page: by itself '
            'while steps are left, closed by the user, or opened again by '
            'the user while anything is left.'
        ),
    )

    # The bot token and the signing secret are for system administrators
    # only: ``res.users`` is readable by every internal user, so without a
    # group anyone read anyone's through ``/web/dataset/call_kw`` (read,
    # search_read) and the export. Their owner sets them in the panel's
    # notification preferences, which store them elevated and only say
    # whether each is set; the senders of jobs and alerts read them
    # elevated (``_get_cloud_telegram_bot_token``,
    # ``_get_cloud_webhook_secret``).
    cloud_telegram_bot_token = EncryptedChar(
        string='Telegram Bot Token',
        groups='base.group_system',
        help='Bot token from @BotFather. Stored encrypted.',
    )
    cloud_telegram_chat_id = fields.Char(
        string='Telegram Chat ID',
        help='Numeric chat or channel ID where the bot sends messages.',
    )
    cloud_webhook_url = fields.Char(
        string='External Webhook URL',
        help='HTTPS endpoint that receives POST notifications on job '
             'state changes. Payloads are signed with HMAC-SHA256 '
             'when a signing secret is configured.',
    )
    cloud_webhook_secret = EncryptedChar(
        string='Webhook Signing Secret',
        groups='base.group_system',
        help='HMAC-SHA256 secret used to sign webhook payloads. '
             'Set to the same value the receiving endpoint expects '
             'in the X-IncubaCloud-Signature header.',
    )

    def _get_cloud_telegram_bot_token(self):
        """Return this user's Telegram bot token, read elevated.

        ``cloud_telegram_bot_token`` is for system administrators only;
        the senders of jobs and alerts, and the buttons of the user's own
        preferences, read it for whoever runs them.

        :return: the token, or ``False`` when none is stored
        """
        self.ensure_one()
        return self.sudo().cloud_telegram_bot_token

    def _get_cloud_webhook_secret(self):
        """Return this user's webhook signing secret, read elevated.

        ``cloud_webhook_secret`` is for system administrators only, as the
        Telegram bot token is; the senders of jobs and alerts sign with it
        for whoever runs them.

        :return: the secret, or ``False`` when none is stored
        """
        self.ensure_one()
        return self.sudo().cloud_webhook_secret

    def _cloud_project_muted(self, project):
        """Return True when *project* is in this user's muted list.

        Falsy/empty projects are never muted — host-level events have
        no project to mute by.
        """
        self.ensure_one()
        return bool(project) and project in self.cloud_muted_project_ids

    @api.model
    def _cron_send_cloud_digest(self):
        """Send the daily cloud digest to every opted-in user.

        Each user runs in its own try/except so one broken mailbox
        cannot starve the rest; the per-user watermark only advances
        after a successful send (or a genuinely empty window).
        """
        now = fields.Datetime.now()
        users = self.search([
            ('share', '=', False),
            ('active', '=', True),
            ('cloud_notification_level', '!=', 'none'),
            ('cloud_notification_mode', '=', 'daily_digest'),
            ('cloud_email_enabled', '=', True),
        ])
        for user in users:
            try:
                self._send_cloud_digest_for(user, now)
            except Exception:
                _logger.exception(
                    'cloud digest failed for user %s', user.id,
                )

    @api.model
    def _send_cloud_digest_for(self, user, now):
        """Build and send one user's digest for ``(watermark, now]``.

        Content is searched *as the user* — the cloud.job/cloud.alert
        record rules stay the single source of truth — and muted
        projects are excluded; the surviving records are then re-read
        with sudo only to format display values (a member may read a
        job without having read access to its host's name).
        Returns True when a mail was actually sent.
        """
        since = user.cloud_last_digest_at or (now - timedelta(days=1))
        muted_ids = user.cloud_muted_project_ids.ids
        CloudJob = self.env['cloud.job']
        job_domain = [
            ('write_date', '>', since),
            ('write_date', '<=', now),
            ('job_type_id.code', 'not in', CloudJob._get_hidden_job_types()),
        ]
        # Only add the mute legs when something is actually muted:
        # a dotted ``not in []`` does NOT mean "match all" — it drops
        # every record (empty-subselect semantics). The explicit
        # ``instance_id = False`` branch keeps host-level jobs, whose
        # NULL instance must never be swallowed by the traversal.
        if muted_ids:
            job_domain += [
                '|',
                ('instance_id', '=', False),
                ('instance_id.project_id', 'not in', muted_ids),
            ]
        if user.cloud_notification_level == 'all':
            job_domain.append(('state', 'in', ('failed', 'done')))
        else:
            job_domain.append(('state', '=', 'failed'))
        alert_domain = [
            ('create_date', '>', since),
            ('create_date', '<=', now),
            # job_failed alerts are redundant here — the digest's job
            # section already lists every failed job with its log URL.
            ('code', '!=', 'job_failed'),
        ]
        if self.env['cloud.alert']._provider_alerts_routed():
            # What the provider was told about is not this operator's
            # morning reading either.
            alert_domain.append(('audience', '=', 'fleet'))
        silent = self.env['cloud.alert']._silent_alert_codes()
        if silent:
            # Listed in the panel, told to nobody: not in the morning
            # either.
            alert_domain.append(('code', 'not in', tuple(silent)))
        if muted_ids:
            alert_domain += [
                '!', '|',
                ('project_id', 'in', muted_ids),
                ('instance_id.project_id', 'in', muted_ids),
            ]
        if user.cloud_notification_level == 'failures':
            alert_domain.append(('level', '=', 'critical'))
        try:
            jobs = CloudJob.with_user(user).search(
                job_domain, order='write_date desc', limit=100,
            )
            alerts = self.env['cloud.alert'].with_user(user).search(
                alert_domain, order='create_date desc', limit=100,
            )
        except AccessError:
            # No cloud ACLs at all — nothing this user could digest.
            jobs = CloudJob
            alerts = self.env['cloud.alert']
        if not jobs and not alerts:
            user.sudo().cloud_last_digest_at = now
            return False
        email = user.partner_id.email
        if not email:
            # No mailbox to deliver to; advancing avoids an unbounded
            # backlog the day the user finally sets an address.
            _logger.warning(
                'cloud digest skipped for user %s: no email set', user.id,
            )
            user.sudo().cloud_last_digest_at = now
            return False
        values = self._cloud_digest_values(
            user, jobs.sudo(), alerts.sudo(), since, now,
        )
        body = wrap_in_mail_layout(self.env, self.env['ir.qweb']._render(
            'incubacloud.mail_cloud_digest', values,
        ))
        subject = (
            f"[IncubaCloud] Daily digest: {values['failed_count']} failed"
            f" job(s), {values['alert_count']} new alert(s)"
        )
        self.env['mail.mail'].sudo().create({
            'subject': subject,
            'body_html': body,
            'email_to': email,
            'auto_delete': True,
        }).send()
        user.sudo().cloud_last_digest_at = now
        return True

    @api.model
    def _cloud_digest_values(self, user, jobs, alerts, since, now):
        """Format sudo'd records into the plain dicts the digest QWeb
        template renders — the template never touches the ORM.
        """
        def _when(dt):
            return fields.Datetime.context_timestamp(
                user, dt,
            ).strftime('%Y-%m-%d %H:%M')

        def _job_target(job):
            if job.instance_id:
                project = job.instance_id.project_id.name
                name = job.instance_id.name
                return f'{project} / {name}' if project else name
            return job.host_id.name or ''

        def _alert_target(alert):
            if alert.host_id:
                return alert.host_id.name
            if alert.instance_id:
                project = alert.instance_id.project_id.name
                name = alert.instance_id.name
                return f'{project} / {name}' if project else name
            return alert.project_id.name or ''

        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '',
        )
        failed, done = [], []
        for job in jobs:
            row = {
                'name': job.name,
                'target': _job_target(job),
                'when': _when(job.write_date),
                'url': f'{base_url}/cloud/log/{job.id}',
            }
            (failed if job.state == 'failed' else done).append(row)
        alert_rows = [
            {
                'level': a.level,
                'message': a.message,
                'target': _alert_target(a),
                'when': _when(a.create_date),
            }
            for a in alerts
        ]
        return {
            'period_start': _when(since),
            'period_end': _when(now),
            'failed_jobs': failed,
            'done_jobs': done,
            'alerts': alert_rows,
            'failed_count': len(failed),
            'done_count': len(done),
            'alert_count': len(alert_rows),
            'console_url': f'{base_url}/cloud/ui',
            'accent': self.env.company.email_secondary_color or '#875A7B',
        }

    @api.model
    def _get_cron_bot(self):
        """Return the platform's own user, or an empty recordset.

        This is the user the scheduled actions already run as. It is
        also the right actor for any unattended work the platform does
        on its own account outside a cron — preparing an instance ahead
        of the request that will use it, say — so ``create_uid`` keeps
        meaning "who wanted this" rather than "who happened to be
        logged in when the platform did its housekeeping".

        Resolved by XML-id with a login fallback, mirroring
        :meth:`_incubacloud_ensure_cron_bot`, and returns empty rather
        than raising: a caller that cannot find the bot should carry on
        with a slightly wrong ``create_uid``, not fail the operation.
        """
        bot = self.env.ref(CRON_BOT_XML_ID, raise_if_not_found=False)
        if bot:
            return bot
        return self.sudo().with_context(active_test=False).search(
            [('login', '=', CRON_BOT_LOGIN)], limit=1,
        )

    @api.model
    def _incubacloud_ensure_cron_bot(self):
        """Create (or re-home) the cron bot user referenced by every
        ``ir.cron`` in this module.

        Called from the post-init hook, once this module's data is loaded,
        and from the post-migrations that give crons to the bot.

        The user and its partner are made with ``create()``, so the
        defaults of every installed module apply. On install the hook runs
        after the modules already installed are loaded, whose fields the
        registry then knows.

        During an update the post-migrations run before the modules that
        load after this one (``account``, ``purchase_stock``...): their
        required columns are in the tables, their fields not yet in the
        registry, and the ``create()`` leaves those columns empty. While
        the registry is not fully loaded the creation is therefore tried
        in a savepoint; when the database refuses it, the bot is left for
        ``migrations/0.0.0/end-cron-bot.py``, which runs with every module
        loaded, and nothing else is done here. Once the registry is
        loaded, a failure raises.

        Idempotent: on second install we find the existing user by
        XML-id (preferred) or by login (fallback for manually
        created users, archived ones included) and only align groups.

        :return: the bot, or an empty recordset when its creation was
            deferred.
        """
        IMD = self.env['ir.model.data'].sudo()
        module, name = CRON_BOT_XML_ID.split('.', 1)
        mdata = IMD.search(
            [('module', '=', module),
             ('name', '=', name),
             ('model', '=', 'res.users')],
            limit=1,
        )
        user = self.browse(mdata.res_id).exists() if mdata else self.browse()
        if not user:
            user = self.sudo().with_context(active_test=False).search(
                [('login', '=', CRON_BOT_LOGIN)], limit=1,
            )

        if not user:
            # Through the ORM, partner included: every installed module's
            # defaults apply, whatever NOT NULL columns those modules add
            # to the users' and partners' tables. An INSERT naming the
            # columns it knew left the others empty and failed.
            vals = {
                'name': 'IncubaCloud Cron Bot',
                'login': CRON_BOT_LOGIN,
                'active': True,
                'share': False,
                # No address: Odoo notifications reach it in the inbox.
                # 'email' turned every message it followed into a failed
                # mail notification.
                'notification_type': 'inbox',
            }
            if self.env.registry.loaded:
                user = self.sudo().create(vals)
            else:
                try:
                    with mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                        user = self.sudo().create(vals)
                except psycopg2.IntegrityError as exc:
                    _logger.warning(
                        "incubacloud cron bot not created yet: a module "
                        "loaded after this one requires a value the "
                        "registry cannot give it (%s); it is created once "
                        "the registry is fully loaded",
                        exc.diag.message_primary,
                    )
                    return self.browse()
            _logger.info("created incubacloud cron bot user id=%s", user.id)

        # Ensure an ``ir.model.data`` row exists so cron XMLs can
        # reference the user by its fully-qualified XML-id regardless
        # of how it got created above (existing-login fallback or
        # fresh create). A row left pointing at a user that is gone
        # points at this one.
        if not mdata:
            IMD.create({
                'module': module,
                'name': name,
                'model': 'res.users',
                'res_id': user.id,
                'noupdate': True,
            })
        elif mdata.res_id != user.id:
            mdata.res_id = user.id

        # Align groups. Re-running is safe (``(4, id)`` is a no-op
        # when the membership already exists). We intentionally do
        # NOT remove groups an operator may have added — only add
        # the ones we need.
        required = [
            self.env.ref('incubacloud.group_cloud_manager').id,
            self.env.ref('base.group_user').id,
            self.env.ref('queue_job.group_queue_job_manager').id,
            # Odoo derives ``notification_type`` from this group, so the
            # 'inbox' set at creation only holds while the bot is in it.
            self.env.ref('mail.group_mail_notification_type_inbox').id,
        ]
        user.sudo().write({'group_ids': [(4, gid) for gid in required]})
        return user

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

        Without a bot (its creation deferred during an update, see
        :meth:`_incubacloud_ensure_cron_bot`) nothing changes: the crons
        keep running as uid 1 until it exists. A cron running right now
        holds its row locked, and writing it would fail the whole
        update: it keeps its user and the next assignment moves it.
        """
        IMD = self.env['ir.model.data'].sudo()
        bot = self.env.ref(CRON_BOT_XML_ID, raise_if_not_found=False)
        if not bot:
            _logger.info(
                "no incubacloud cron bot yet: the %s crons keep their user",
                module_name,
            )
            return
        cron_mdata = IMD.search([
            ('module', '=', module_name),
            ('model', '=', 'ir.cron'),
        ])
        if not cron_mdata:
            return
        module_crons = self.env['ir.cron'].sudo().browse(
            cron_mdata.mapped('res_id'),
        ).exists()
        stale = module_crons.filtered(lambda c: c.user_id.id == 1)
        if stale:
            # The rows a running cron does not hold, locked as the cron
            # runner locks them. Plain SQL: ``try_lock_for_update`` is not
            # in Odoo 18.0.
            self.env.cr.execute(
                "SELECT id FROM ir_cron WHERE id IN %s"
                " FOR NO KEY UPDATE SKIP LOCKED",
                [tuple(stale.ids)],
            )
            locked = {row[0] for row in self.env.cr.fetchall()}
            free = stale.filtered(lambda c: c.id in locked)
            if stale - free:
                _logger.warning(
                    "left %d running %s crons on their user: %s",
                    len(stale - free), module_name,
                    ', '.join((stale - free).mapped('cron_name')),
                )
            if free:
                free.write({'user_id': bot.id})
                _logger.info(
                    "reassigned %d %s crons to cron bot user",
                    len(free), module_name,
                )

        # Odoo 19 hardened ``ir.actions.server.run()``: when an action
        # carries no ``group_ids``, ``_can_execute_action_on_records``
        # requires the executing user to have *write* access on the
        # action's model. The cron bot is a cloud manager but deliberately
        # lacks write on append-only / abstract orchestration models
        # (audit log, rate-limit buckets, terminal routes, and the helper
        # AbstractModels of layered modules), so those crons
        # raised AccessError before their internal ``sudo()`` ever ran.
        # Scoping each bot-owned cron's server action to a group the bot
        # belongs to makes the check take the group-membership branch,
        # authorising the run without granting broad table write.
        bot_crons = module_crons.filtered(lambda c: c.user_id.id == bot.id)
        if bot_crons:
            manager_group = self.env.ref('incubacloud.group_cloud_manager')
            bot_crons.ir_actions_server_id.write({
                'group_ids': [(4, manager_group.id)],
            })
