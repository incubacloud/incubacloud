"""Tests for the incubacloud cron bot user (P1.26)."""
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.res_users_ext import (
    INCUBACLOUD_SUPERUSER_CRONS,
)


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
        self.assertIn(g_manager, self.bot.groups_id)

    def test_user_is_NOT_system_administrator(self):
        """Scope check: if the bot ever inherits ``base.group_system``
        (superuser), the whole exercise is pointless. Catch it here."""
        g_system = self.env.ref('base.group_system')
        self.assertNotIn(
            g_system, self.bot.groups_id,
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
        runs as the bot — except the maintenance crons that intentionally
        run as OdooBot (``INCUBACLOUD_SUPERUSER_CRONS``). Regression guard
        to make sure we did not introduce a cron that forgets to switch
        user."""
        IMD = self.env['ir.model.data']
        incubacloud_crons = IMD.search([
            ('module', '=', 'incubacloud'),
            ('model', '=', 'ir.cron'),
        ])
        bot_crons = incubacloud_crons.filtered(
            lambda m: m.name not in INCUBACLOUD_SUPERUSER_CRONS
        )
        su_crons = incubacloud_crons.filtered(
            lambda m: m.name in INCUBACLOUD_SUPERUSER_CRONS
        )
        # Bot-owned crons must point at the bot. Deleted crons (unlinked
        # via upgrade) may leave IMD rows for a cycle — skip with .exists().
        wrong = self.env['ir.cron'].browse(
            bot_crons.mapped('res_id'),
        ).exists().filtered(lambda c: c.user_id.id != self.bot.id)
        self.assertFalse(
            wrong,
            "cron(s) not assigned to cron bot: %s" % wrong.mapped('cron_name'),
        )
        # Maintenance crons must run as OdooBot (uid 1) so Odoo 18's
        # server-action write pre-check passes without widening manager ACLs.
        wrong_su = self.env['ir.cron'].browse(
            su_crons.mapped('res_id'),
        ).exists().filtered(lambda c: c.user_id.id != 1)
        self.assertFalse(
            wrong_su,
            "maintenance cron(s) not running as OdooBot: %s"
            % wrong_su.mapped('cron_name'),
        )
