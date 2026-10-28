"""Section 12A of the Terms and the code say the same about what waits (28-oct).

«Actions that delete or restore data, incur charges, deploy to production,
write to an existing branch or change security settings only run after that
user confirms them in the panel; the AI model cannot confirm them by itself.»
(terms §12A, approved by the owner on 28-oct-2026). Every change asks except
``_UNASKED`` on a staging: none of those may be of these kinds. Changing
either one means reviewing the other, and this file.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.assistant_tools import _UNASKED
from .common import AssistantMixin

#: The changes that run without a card on a staging, and why none is of §12A.
_REVIEWED_UNASKED = {
    "deploy_instance": "a staging; production asks",
    "rebuild_instance": "a staging's image; production asks",
    "keep_staging": "keeps a staging from its purge",
    "create_backup": "adds a backup: deletes nothing, charges nothing",
    "dismiss_alerts": "hides one alert: no data is deleted; all of them asks",
    "manage_tags": "lists or creates tags",
    "repo_create_branch": "a new branch; an existing one keeps the MCP's approval",
    "repo_write_files": "the agent's new branch; an existing one keeps the MCP's "
    "approval",
    "repo_open_pull_request": "opens a pull request: merges nothing",
}
#: Tools of the kinds §12A names that always ask, in every mode.
_ALWAYS_ASK = (
    "delete_instance",
    "delete_project",
    "delete_backup_backend",
    "restore_backup",
    "refresh_staging",
    "clear_staging_mailbox",
    "update_settings",
)


@tagged("post_install", "-at_install")
class TestSection12aMatchesTheCode(AssistantMixin, TransactionCase):
    """The few unasked changes are none of §12A's; §12A's kinds ask."""

    def setUp(self):
        """A manager and a developer on the panel's fixture."""
        super().setUp()
        self._panel_fixture()
        self.manager = self._cloud_user("asis-12a-m", group="group_cloud_manager")
        self.developer = self._cloud_user("asis-12a-d")
        self.project.member_ids = [(4, self.developer.id)]

    def test_the_unasked_changes_are_the_reviewed_ones(self):
        """A tool added to or taken from ``_UNASKED`` is reviewed against §12A."""
        self.assertEqual(set(_UNASKED), set(_REVIEWED_UNASKED))

    def test_what_section_12a_names_asks(self):
        """Deleting, restoring, emptying a mailbox and the settings: a card."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.manager)
        Catalog = self.env["incubacloud.mcp.catalog"]
        for name in _ALWAYS_ASK:
            with self.subTest(name):
                self.assertTrue(Tools._extra_confirmation(Catalog._spec(name), {}))

    def test_deploying_or_rebuilding_production_asks(self):
        """The unasked deploys hold for a staging only."""
        Tools = self.env["incubacloud.assistant.tools"].with_user(self.developer)
        Catalog = self.env["incubacloud.mcp.catalog"]
        for name in ("deploy_instance", "rebuild_instance"):
            with self.subTest(name):
                self.assertTrue(
                    Tools._extra_confirmation(
                        Catalog._spec(name), {"instance_id": self.production.id}
                    )
                )

    def test_emptying_a_staging_s_mailbox_says_what_goes(self):
        """Its own text: the staging, and that the mail is deleted for good."""
        spec = self.env["incubacloud.mcp.catalog"]._spec("clear_staging_mailbox")
        card = (
            self.env["incubacloud.assistant.tools"]
            .with_user(self.developer)
            ._extra_confirmation(spec, {"instance_id": self.staging.id})
        )
        self.assertIn(self.staging.name, card)
        self.assertIn("deleted for good", card)
