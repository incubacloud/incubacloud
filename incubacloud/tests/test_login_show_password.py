"""The show password button of /web/login: named, with its state and a visible focus.

The button is an icon with no text. A screen reader said "button" and
nothing else, and tabbing to it drew no focus. It now carries a name,
``aria-pressed`` that follows what it toggles, and a visible focus.
"""
from lxml import html

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests._browser import ChromeStartRetry

_JS = """
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(check, what, timeout = 20000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
        const found = check();
        if (found) {
            return found;
        }
        await sleep(100);
    }
    throw new Error("timed out waiting for " + what);
}
"""


@tagged('post_install', '-at_install')
class TestLoginShowPassword(ChromeStartRetry, HttpCase):
    """What the login page serves and what a click and the keyboard do to it."""

    def _button(self, lang=None):
        """Return the show password button of the login page as served.

        :param str lang: a language code to browse in (its cookie), or
            None for the default.
        :return: the ``lxml`` element of ``button.o_show_password``.
        """
        if lang:
            self.opener.cookies.set('frontend_lang', lang)
        response = self.url_open('/web/login')
        self.assertEqual(response.status_code, 200)
        buttons = html.fromstring(response.content).cssselect(
            'form.oe_login_form button.o_show_password')
        self.assertEqual(len(buttons), 1, 'one show password button on the login')
        return buttons[0]

    def test_the_button_has_a_name_and_a_state(self):
        """The served button names its action, says it is not pressed, hides its icon."""
        button = self._button()
        self.assertEqual(button.get('aria-label'), 'Show password')
        self.assertEqual(button.get('aria-pressed'), 'false')
        self.assertEqual(button.get('aria-controls'), 'password')
        self.assertEqual(button.get('type'), 'button')
        self.assertEqual(button.cssselect('i')[0].get('aria-hidden'), 'true')

    def test_the_name_is_translated(self):
        """A Spanish visitor's screen reader hears the Spanish name.

        Read from the template's own arch in Spanish: which language a
        visitor browses in depends on the website, when one is installed.
        """
        self.env['res.lang']._activate_lang('es_ES')
        self.env['ir.module.module']._load_module_terms(
            ['incubacloud'], ['es_ES'])
        view = self.env.ref('incubacloud.login_show_password_name')
        self.assertIn(
            'Mostrar contraseña', view.with_context(lang='es_ES').arch_db)

    def test_a_click_toggles_the_state_and_the_focus_shows(self):
        """Two clicks: the type of the field and aria-pressed go together, and focus shows."""
        self.browser_js('/web/login', _JS + """
        (async () => {
            await waitFor(() => !document.body.classList.contains('o_lazy_js_waiting'), 'the lazy JS');
            const button = await waitFor(
                () => document.querySelector('.oe_login_form .o_show_password[aria-pressed]'),
                'the button');
            const input = document.querySelector('#password');
            const check = (type, pressed, when) => {
                if (input.type !== type || button.getAttribute('aria-pressed') !== pressed) {
                    throw new Error(when + ': type ' + input.type + ', aria-pressed '
                        + button.getAttribute('aria-pressed'));
                }
            };
            check('password', 'false', 'at the start');
            // The interactions start after the page is complete, and nothing
            // says when: click until the first one has an effect.
            await waitFor(() => {
                if (input.type !== 'text') {
                    button.click();
                }
                return input.type === 'text';
            }, 'the password shown');
            await sleep(50);
            check('text', 'true', 'after a click');
            button.click();
            await waitFor(() => input.type === 'password', 'the password hidden');
            await sleep(50);
            check('password', 'false', 'after a second click');
            button.focus();
            if (document.activeElement !== button) {
                throw new Error('the button cannot take the focus');
            }
            const outline = getComputedStyle(button).outlineStyle;
            if (outline === 'none') {
                throw new Error('the focused button draws no outline');
            }
            console.log('test successful');
        })().catch((error) => console.error(String(error)));
        """, timeout=90)
