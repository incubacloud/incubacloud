"""Tests for the incubacloud cron bot user (P1.26)."""
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestCronBotUser(TransactionCase):
    """Sanity checks on the system user crons run as."""

    def setUp(self):
        super().setUp()
        self.bot = self.env.ref(
            'incubacloud.user_incubacloud_cron',
        )

    def test_user_exists_with_expected_login(self):
        self.assertTrue(self.bot)
        self.assertEqual(self.bot.login, '__incubacloud_cron__')
        self.assertTrue(self.bot.active)
        self.assertFalse(self.bot.share)

    def test_user_is_in_cloud_manager_group(self):
        g_manager = self.env.ref('incubacloud.group_cloud_manager')
        self.assertIn(g_manager, self.bot.all_group_ids)

    def test_user_is_NOT_system_administrator(self):
        """Scope check: if the bot ever inherits ``base.group_system``
        (superuser), the whole exercise is pointless. Catch it here."""
        g_system = self.env.ref('base.group_system')
        self.assertNotIn(
            g_system, self.bot.all_group_ids,
            "cron bot must not inherit base.group_system",
        )

    def test_user_can_read_cloud_models(self):
        """Scope check: bot must be allowed to read the models the
        crons it will run actually touch."""
        host = self.env['cloud.host'].with_user(self.bot).search([], limit=1)
        # No exception = read permission OK. Empty recordset is fine.
        self.assertFalse(host.exists() and False)

    def test_all_module_crons_run_as_bot(self):
        """Every ``ir.cron`` whose xml-id starts with ``incubacloud.``
        runs as the bot — a regression guard to make sure we did not
        introduce a cron that forgets to switch user."""
        IMD = self.env['ir.model.data']
        incubacloud_crons = IMD.search([
            ('module', '=', 'incubacloud'),
            ('model', '=', 'ir.cron'),
        ])
        # Every record that exists must have user_id == bot. Deleted
        # crons (unlinked via upgrade) may leave IMD rows for a cycle
        # — skip them with .exists().
        wrong = self.env['ir.cron'].browse(
            incubacloud_crons.mapped('res_id'),
        ).exists().filtered(lambda c: c.user_id.id != self.bot.id)
        self.assertFalse(
            wrong,
            "cron(s) not assigned to cron bot: %s" % wrong.mapped('cron_name'),
        )

    def test_all_module_crons_pass_server_action_precheck(self):
        """Every module cron must satisfy Odoo 19's hardened server-action
        access gate when run as the bot.

        ``ir.actions.server._can_execute_action_on_records`` authorises a
        run only if the action is scoped to a group the executing user
        shares, *or* the user has write access to the action's model.
        Crons targeting append-only / abstract models (audit log,
        rate-limit, terminal routes, warm-pool & provisioner helpers)
        carried neither and failed with ``AccessError`` on every tick.

        We replicate that gate's logic here — reading the action with
        ``sudo()`` so we test the bot's access to the *model*, not the
        (unrelated) read ACL on ``ir.actions.server`` itself.
        """
        IMD = self.env['ir.model.data']
        cron_mdata = IMD.search([
            ('module', '=', 'incubacloud'),
            ('model', '=', 'ir.cron'),
        ])
        crons = self.env['ir.cron'].browse(
            cron_mdata.mapped('res_id'),
        ).exists()
        offenders = []
        for cron in crons:
            action = cron.ir_actions_server_id.sudo()
            groups = action.group_ids
            if groups:
                if not (groups & cron.user_id.all_group_ids):
                    offenders.append(
                        "%s: user %s is in none of the action groups %s"
                        % (cron.cron_name, cron.user_id.login,
                           groups.mapped('full_name'))
                    )
                continue
            # No group restriction → the run requires model write access.
            model = action.model_id.model
            try:
                self.env[model].with_user(cron.user_id).check_access('write')
            except AccessError:
                offenders.append(
                    "%s: user %s lacks write on %s and the action has no group"
                    % (cron.cron_name, cron.user_id.login, model)
                )
        self.assertFalse(
            offenders,
            "cron(s) fail the Odoo 19 server-action access gate:\n%s"
            % "\n".join(offenders),
        )
