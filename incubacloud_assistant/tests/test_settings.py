"""The assistant is off until a manager turns it on, for the users chosen."""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.protocol.redact import MASK

from ..models.cloud_settings import BLOCKED_PARAM
from .common import KEY, AssistantMixin


@tagged("post_install", "-at_install")
class TestAssistantSettings(AssistantMixin, TransactionCase):
    """Who may use the assistant, and its key as a secret."""

    def setUp(self):
        """A developer and a manager of the panel."""
        super().setUp()
        self.developer = self._cloud_user("asis-set-dev")
        self.manager = self._cloud_user("asis-set-mgr", group="group_cloud_manager")
        self.Settings = self.env["cloud.settings"]

    def test_off_by_default(self):
        """A fresh panel: off, no key, nobody, 30 steps."""
        config = self.Settings._assistant_config()
        self.assertFalse(config.enabled)
        self.assertFalse(config.has_key)
        self.assertEqual(config.user_ids, frozenset())
        self.assertEqual(config.step_cap, 30)
        self.assertFalse(self.Settings._assistant_available_for(self.manager))

    def test_only_the_users_chosen_one_by_one(self):
        """On, with a key and a model: only the users chosen may use it."""
        self._configure(users=[self.developer])
        self.assertTrue(self.Settings._assistant_available_for(self.developer))
        self.assertFalse(self.Settings._assistant_available_for(self.manager))

    def test_off_without_a_key_a_model_or_the_switch(self):
        """Each missing piece turns it off for everybody."""
        for change in ({"enabled": False}, {"key": False}, {"default": ""}):
            with self.subTest(change):
                self._configure(users=[self.developer], **change)
                self.assertFalse(self.Settings._assistant_available_for(self.developer))

    def test_the_key_is_a_secret_the_filter_masks(self):
        """An encrypted field: its name is refused in arguments, its value masked."""
        self._configure(users=[self.developer])
        Secrets = self.env["incubacloud.mcp.secrets"]
        self.assertIn("assistant_api_key", Secrets._field_names())
        self.assertEqual(Secrets._scrubber().text(f"key: {KEY}"), f"key: {MASK}")
        self.assertEqual(self.Settings._assistant_key(), KEY)

    def test_a_database_that_blocks_it_never_has_it(self):
        """IncubaCloud's own panel: configured and chosen, still not available."""
        self._configure(users=[self.developer])
        self.env["ir.config_parameter"].sudo().set_param(
            BLOCKED_PARAM, "Not offered on this panel."
        )
        self.assertEqual(
            self.Settings._assistant_blocked_reason(), "Not offered on this panel."
        )
        self.assertFalse(self.Settings._assistant_available_for(self.developer))

    def test_a_step_cap_that_is_not_positive_is_30(self):
        """Zero or less falls back to the default."""
        self._configure(users=[self.developer], step_cap=0)
        self.assertEqual(self.Settings._assistant_config().step_cap, 30)

    def test_a_listing_that_is_not_json_reads_as_empty(self):
        """A broken listing never takes the panel down."""
        self._configure(users=[self.developer])
        self.Settings._get_system().assistant_models = "{not json"
        self.assertEqual(self.Settings._assistant_config().models, ())
