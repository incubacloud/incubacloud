"""Structural guards for the pluggable action buttons.

Two defects hid the same button twice. It rendered as a bare icon in a
bar where every other control carries its name, so the operator could
not find the action at all — next to "Re-trust Key", which is also a
shield, an unlabelled shield is unreadable. And the handler received
only the job type's code, so the confirmation notice the record
carries had nowhere to arrive: a job that restarts the proxy serving
this panel ran with no warning, and the "connection interrupted" that
followed read as a broken button.

Nothing in the Python suite renders a pixel and the hoot tests are not
part of any gate, so both checks are textual — the same approach as the
theme and shared-helper guards.
"""
import pathlib

from odoo.tests.common import BaseCase

_COMPONENTS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static" / "src" / "components"
)

#: The two bars that render ``custom_actions``.
_BARS = (
    ("host_detail", "hostAction"),
    ("instance_detail", "instanceAction"),
)


class TestActionBarLabels(BaseCase):

    def _template(self, component):
        return (_COMPONENTS / component / f"{component}.xml").read_text()

    def _script(self, component):
        return (_COMPONENTS / component / f"{component}.js").read_text()

    def test_custom_action_buttons_show_their_name(self):
        for component, _handler in _BARS:
            with self.subTest(component=component):
                self.assertIn(
                    't-esc="action.name"',
                    self._template(component),
                    "A custom action button must render its name, not "
                    "just an icon: the operator cannot find it "
                    "otherwise.",
                )

    def test_handler_receives_the_whole_action(self):
        """The notice travels on the record, so the code alone is not enough."""
        for component, handler in _BARS:
            with self.subTest(component=component):
                self.assertIn(
                    f"this.{handler}(action)",
                    self._template(component),
                    "Pass the action object so its confirmation notice "
                    "reaches the handler.",
                )

    def test_both_handlers_honour_the_notice(self):
        for component, _handler in _BARS:
            with self.subTest(component=component):
                self.assertIn(
                    "action.action_confirm",
                    self._script(component),
                    "A job type declaring a notice must be confirmed "
                    "before it is queued.",
                )

    def test_host_bar_guards_against_a_second_press(self):
        """The host bar was the one without a guard; instance_detail has
        its own in ``_enqueueJob``."""
        script = self._script("host_detail")
        self.assertIn("if (this.state.actionBusy) return;", script)
        self.assertIn("this.state.actionBusy = action.code;", script)

    def test_host_bar_refreshes_without_waiting_for_the_bus(self):
        """The bus connection is the next casualty of this very job."""
        script = self._script("host_detail")
        queued = script.split("async hostAction(action)", 1)[1]
        self.assertIn("this._silentRefresh();", queued.split("\n    }", 1)[0])
