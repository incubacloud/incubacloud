"""Tests for the incubacloud cron bot user (P1.26)."""
import importlib.util
import logging
from pathlib import Path
from unittest.mock import patch

import psycopg2

import odoo
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import SQL, mute_logger

from ..models.res_users_ext import CRON_BOT_LOGIN, CRON_BOT_XML_ID

# Captured from ``logging.INFO``, not ``"INFO"``: some builds of Odoo 19
# name its level 25 "INFO", and the name then stands for 25.
_LOGGER = "odoo.addons.incubacloud.models.res_users_ext"
_END_SCRIPT = Path(__file__).resolve().parents[1] / "migrations" / "0.0.0" / "end-cron-bot.py"
_END_LOGGER = "odoo.addons.incubacloud.migrations.end_cron_bot"


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
        rate-limit, terminal routes, layered modules' helpers)
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


@tagged("post_install", "-at_install")
class TestCronBotCreation(TransactionCase):
    """The bot is created through the ORM, so every module's defaults apply.

    Other modules add NOT NULL columns with an ORM default to the users'
    and partners' tables (on Odoo.sh, ``group_rfq`` and ``group_on``); the
    panel cannot know which. The test adds one of its own as an unknown
    module would: a field the registry knows, its default applied by the
    ORM, its column NOT NULL. Post-install, as on a database where those
    modules are loaded.
    """

    FIELD = "x_ic_unknown_module_flag"

    def setUp(self):
        """Add the unknown module's NOT NULL column and move the bot there aside."""
        super().setUp()
        self.addCleanup(self.registry.reset_changes)
        self.addCleanup(self.registry.clear_all_caches)
        self.env["ir.model.fields"].create({
            "model_id": self.env["ir.model"]._get_id("res.partner"),
            "name": self.FIELD,
            "field_description": "Flag of a module the panel does not know",
            "ttype": "char",
        })
        self.env["ir.default"].set("res.partner", self.FIELD, "from-its-default")
        column = SQL.identifier(self.FIELD)
        self.env.cr.execute(SQL("UPDATE res_partner SET %s = 'existing'", column))
        self.env.cr.execute(SQL("ALTER TABLE res_partner ALTER COLUMN %s SET NOT NULL", column))
        # The bot already there steps aside, so the next call creates one.
        old = self.env.ref("incubacloud.user_incubacloud_cron")
        self.env["ir.model.data"].search([
            ("module", "=", "incubacloud"), ("name", "=", "user_incubacloud_cron"),
        ]).unlink()
        old.login = "__incubacloud_cron_old__"

    def test_the_bot_is_created_with_the_other_modules_defaults(self):
        """The new bot's partner holds the column's default; login, groups as before."""
        self.env["res.users"]._incubacloud_ensure_cron_bot()
        bot = self.env.ref("incubacloud.user_incubacloud_cron")
        self.assertEqual(bot.login, "__incubacloud_cron__")
        self.assertEqual(bot.name, "IncubaCloud Cron Bot")
        self.assertEqual(bot.partner_id[self.FIELD], "from-its-default")
        self.assertIn(self.env.ref("incubacloud.group_cloud_manager"), bot.all_group_ids)
        self.assertEqual(bot.notification_type, "inbox")


@tagged("post_install", "-at_install")
class TestCronBotDeferred(TransactionCase):
    """A bot the registry cannot create yet is left for the end of the update.

    During an update this module's post-migrations run before the modules
    loaded after it, whose required columns are in the tables but whose
    fields are not in the registry. The test makes such a column on
    ``res_partner`` with SQL, a column no field knows, and marks the
    registry as still loading.
    """

    GHOST = "x_ic_ghost_required"

    def setUp(self):
        """Clear the caches the SQL of the tests leaves stale."""
        super().setUp()
        self.addCleanup(self.registry.reset_changes)
        self.addCleanup(self.registry.clear_all_caches)
        self.Users = self.env["res.users"]
        self.bot = self.env.ref(CRON_BOT_XML_ID)

    def _ghost_column(self):
        """Add a NOT NULL column to ``res_partner`` that no field knows."""
        column = SQL.identifier(self.GHOST)
        self.env.cr.execute(SQL(
            "ALTER TABLE res_partner ADD COLUMN %s int NOT NULL DEFAULT 0", column,
        ))
        self.env.cr.execute(SQL("ALTER TABLE res_partner ALTER COLUMN %s DROP DEFAULT", column))

    def _drop_ghost_column(self):
        """Remove the column of :meth:`_ghost_column`."""
        self.env.cr.execute(SQL(
            "ALTER TABLE res_partner DROP COLUMN %s", SQL.identifier(self.GHOST),
        ))

    def _remove_bot(self):
        """Make the bot missing, with SQL, as a database that lost it.

        Its crons go back to uid 1, its XML-id goes and its login changes,
        so neither lookup finds it. Deleting the row itself is refused by
        the rows that point to the user on a panel's base.

        :return: the ids of the crons that were on the bot
        """
        cr = self.env.cr
        cr.execute("UPDATE ir_cron SET user_id = 1 WHERE user_id = %s RETURNING id", [self.bot.id])
        crons = [row[0] for row in cr.fetchall()]
        module, name = CRON_BOT_XML_ID.split(".", 1)
        cr.execute(
            "DELETE FROM ir_model_data WHERE module = %s AND name = %s", [module, name],
        )
        cr.execute(
            "UPDATE res_users SET login = %s WHERE id = %s",
            [f"__incubacloud_cron_gone_{self.bot.id}__", self.bot.id],
        )
        self.env.invalidate_all()
        self.registry.clear_cache()
        return crons

    def _new_bot(self):
        """Return the bot the XML-id names now, or ``None``."""
        self.registry.clear_cache()
        return self.env.ref(CRON_BOT_XML_ID, raise_if_not_found=False)

    def test_while_loading_a_refused_creation_is_deferred(self):
        """Empty, a warning naming the column, no XML-id, the transaction alive."""
        self._ghost_column()
        self._remove_bot()
        with patch.object(self.registry, "loaded", False), \
                self.assertLogs(_LOGGER, "WARNING") as logs:
            bot = self.Users._incubacloud_ensure_cron_bot()
        self.assertFalse(bot)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn(self.GHOST, logs.output[0])
        self.assertIn("once the registry is fully loaded", logs.output[0])
        self.assertIsNone(self._new_bot())
        # The savepoint undid the failed INSERT: the transaction goes on.
        self.assertFalse(self.Users.search([("login", "=", CRON_BOT_LOGIN)]))

    def test_once_loaded_the_bot_is_created(self):
        """Deferred while loading, created with the registry loaded."""
        self._ghost_column()
        self._remove_bot()
        with patch.object(self.registry, "loaded", False), \
                self.assertLogs(_LOGGER, "WARNING"):
            self.assertFalse(self.Users._incubacloud_ensure_cron_bot())
        self._drop_ghost_column()
        bot = self.Users._incubacloud_ensure_cron_bot()
        self.assertTrue(bot)
        self.assertNotEqual(bot, self.bot)
        self.assertEqual(self._new_bot(), bot)
        self.assertEqual(bot.login, CRON_BOT_LOGIN)
        self.assertIn(self.env.ref("incubacloud.group_cloud_manager"), bot.all_group_ids)

    def test_once_loaded_a_refused_creation_raises(self):
        """With the registry loaded the failure is a real one and is not hidden."""
        self._ghost_column()
        self._remove_bot()
        with self.assertRaises(psycopg2.IntegrityError), \
                mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.Users._incubacloud_ensure_cron_bot()

    def test_an_xml_id_left_on_a_deleted_user_is_moved_to_the_new_bot(self):
        """The XML-id row of a user deleted with SQL names the bot created after."""
        module, name = CRON_BOT_XML_ID.split(".", 1)
        self.env.cr.execute(
            "UPDATE ir_model_data SET res_id = (SELECT max(id) + 1000 FROM res_users) "
            "WHERE module = %s AND name = %s", [module, name],
        )
        self.env.cr.execute(
            "UPDATE res_users SET login = 'gone' || id WHERE id = %s", [self.bot.id],
        )
        self.env.invalidate_all()
        self.registry.clear_cache()
        bot = self.Users._incubacloud_ensure_cron_bot()
        self.assertTrue(bot)
        self.assertNotEqual(bot, self.bot)
        self.assertEqual(self._new_bot(), bot)

    def test_assign_without_a_bot_leaves_the_crons_quietly(self):
        """No bot: no error, the crons stay on uid 1."""
        crons = self._remove_bot()
        self.assertTrue(crons)
        with self.assertLogs(_LOGGER, logging.INFO) as logs:
            self.Users._incubacloud_assign_cron_user_id(module_name="incubacloud")
        self.assertIn("no incubacloud cron bot yet", logs.output[0])
        self.assertEqual(
            set(self.env["ir.cron"].browse(crons).mapped("user_id.id")), {1},
        )

    def test_the_login_fallback_finds_an_archived_bot(self):
        """An archived bot without XML-id is the bot: no second one, no unique error."""
        module, name = CRON_BOT_XML_ID.split(".", 1)
        self.env.cr.execute(
            "DELETE FROM ir_model_data WHERE module = %s AND name = %s", [module, name],
        )
        self.env.cr.execute("UPDATE res_users SET active = false WHERE id = %s", [self.bot.id])
        self.env.invalidate_all()
        self.registry.clear_cache()
        self.assertEqual(self.Users._get_cron_bot(), self.bot)
        self.assertEqual(self.Users._incubacloud_ensure_cron_bot(), self.bot)
        self.assertEqual(self._new_bot(), self.bot)

    def _run_end_script(self):
        """Run ``migrations/0.0.0/end-cron-bot.py`` on this cursor, from 1.0.233."""
        spec = importlib.util.spec_from_file_location(_END_LOGGER, _END_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "19.0.1.0.233")

    def test_the_end_script_repairs_a_missing_bot(self):
        """It creates the bot and gives it every incubacloud% cron left on uid 1."""
        crons = self._remove_bot()
        self._run_end_script()
        bot = self._new_bot()
        self.assertTrue(bot)
        self.assertNotEqual(bot, self.bot)
        self.assertTrue(self.env["ir.cron"].browse(crons).filtered(lambda c: c.user_id == bot))
        IMD = self.env["ir.model.data"]
        left = self.env["ir.cron"].browse(IMD.search([
            ("model", "=", "ir.cron"), ("module", "=like", "incubacloud%"),
        ]).mapped("res_id")).exists().filtered(lambda c: c.user_id.id == 1)
        self.assertFalse(left, left.mapped("cron_name"))

    def test_the_end_script_leaves_a_bot_that_is_there(self):
        """With the bot there it neither creates nor reassigns anything."""
        Users = type(self.Users)
        with patch.object(Users, "_incubacloud_ensure_cron_bot", autospec=True) as ensure:
            self._run_end_script()
        ensure.assert_not_called()

    def test_the_end_script_never_breaks_the_update(self):
        """A failure is an ERROR in the log, and the update goes on."""
        self._remove_bot()
        Users = type(self.Users)
        with patch.object(Users, "_incubacloud_ensure_cron_bot", autospec=True,
                          side_effect=RuntimeError("refused")), \
                self.assertLogs(_END_LOGGER, "ERROR") as logs:
            self._run_end_script()
        self.assertIn("creating it at the end of the update failed", logs.output[0])
        self.env.cr.execute("SELECT 1")


@tagged("post_install", "-at_install")
class TestCronBotAssignSkipsRunningCrons(TransactionCase):
    """A cron that runs holds its row: the assignment leaves it, it does not fail.

    ``ir.cron.write`` raises on a row another transaction holds, which
    would fail the whole update. The row is held from a connection of its
    own, as the cron runner does. That connection only sees committed rows,
    where every cron of this module is already on the bot, so the test
    assigns the crons of a module that ships several on uid 1 (``base``'s,
    on a fresh base).
    """

    def _module_with_crons_on_uid_1(self):
        """Return a module and two or more of its crons committed on uid 1."""
        self.env.cr.execute("""
            SELECT d.module, array_agg(c.id ORDER BY c.id)
              FROM ir_model_data d JOIN ir_cron c ON c.id = d.res_id
             WHERE d.model = 'ir.cron' AND c.user_id = 1
          GROUP BY d.module HAVING count(*) >= 2
          ORDER BY d.module = 'base' DESC, d.module
             LIMIT 1
        """)
        row = self.env.cr.fetchone()
        if not row:
            self.skipTest("no module has two crons on uid 1 in this base")
        return row[0], self.env["ir.cron"].browse(row[1])

    def test_a_locked_cron_keeps_its_user_and_the_others_move(self):
        module, crons = self._module_with_crons_on_uid_1()
        running, others = crons[0], crons[1:]
        cr = odoo.sql_db.db_connect(self.env.cr.dbname).cursor()
        self.addCleanup(cr.close)
        self.addCleanup(cr.rollback)
        cr.execute("SET LOCAL lock_timeout = '10s'")
        cr.execute("SELECT id FROM ir_cron WHERE id = %s FOR NO KEY UPDATE", [running.id])
        self.assertEqual(cr.fetchone()[0], running.id, "the second connection holds the row")
        bot = self.env.ref(CRON_BOT_XML_ID)
        with self.assertLogs(_LOGGER, logging.INFO) as logs:
            self.env["res.users"]._incubacloud_assign_cron_user_id(module_name=module)
        self.assertEqual(running.user_id.id, 1)
        self.assertEqual(set(others.mapped("user_id")), {bot})
        output = "\n".join(logs.output)
        self.assertIn(f"left 1 running {module} crons on their user: {running.cron_name}", output)
        left = [r for r in logs.records if r.getMessage().startswith("left 1 running")]
        self.assertEqual([r.levelname for r in left], ["WARNING"])
        self.assertIn(f"reassigned {len(others)} {module} crons", output)


@tagged("post_install", "-at_install")
class TestCronBotAfterTheUpdate(TransactionCase):
    """The bot as an update leaves it, whichever modules the database has.

    On a database with ``account`` and ``purchase_stock`` that lost its bot,
    an update creates it at its end: with its XML-id, the defaults of those
    modules on its partner and this module's crons.
    """

    def test_the_bot_has_its_xml_id_its_defaults_and_the_crons(self):
        bot = self.env.ref(CRON_BOT_XML_ID)
        self.assertEqual(bot.login, CRON_BOT_LOGIN)
        self.assertIn(self.env.ref("incubacloud.group_cloud_manager"), bot.all_group_ids)
        Partner = self.env["res.partner"]
        for field in ("autopost_bills", "group_rfq", "group_on"):
            if field in Partner._fields:
                with self.subTest(field=field):
                    self.assertEqual(
                        bot.partner_id[field], Partner.default_get([field])[field],
                    )
        IMD = self.env["ir.model.data"]
        crons = self.env["ir.cron"].browse(IMD.search([
            ("model", "=", "ir.cron"), ("module", "=", "incubacloud"),
        ]).mapped("res_id")).exists()
        self.assertTrue(crons)
        self.assertEqual(set(crons.mapped("user_id")), {bot})
