from odoo.tests.common import BaseCase

from ..models.assistant_prompt import system_prompt


class TestSystemPrompt(BaseCase):
    def test_the_person_s_message_sets_the_language_before_the_panel(self):
        """The panel's language is only the fallback of the reply's."""
        prompt = system_prompt("English")
        self.assertIn("in the language of the person's last message", prompt)
        self.assertIn("when it is unclear, in English", prompt)
        self.assertNotIn("The person writes in English", prompt)
