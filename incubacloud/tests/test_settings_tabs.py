"""What is typed on a Settings tab stays there until it is saved.

The Rates tab — and saas's SaaS and Providers — is a component of its
own: its own form, and a save handler it registers with Settings. The
tabs were drawn with ``t-if``, so leaving one unmounted it: its edits
went, and its save handler with them. Back on the tab the stored values
showed again, and Save pressed on another tab never sent the edits. A
tab now stays mounted once opened, hidden while another one shows.

Settings did not count those edits as unsaved either: no hint, and
leaving the screen did not ask. And Save sent every tab whose handler
was registered, changed or not. A tab now registers its state once
mounted, if it loaded: Settings sends it only when its form changed,
and counts the change as unsaved until then. What is saved is what was
sent: an edit made while the request is out stays unsaved. A tab that
failed to load shows the error and Retry, not a form of defaults, and
is never sent; and one cancelled while it loaded — Settings drew again
meanwhile — never stands in for the one on screen.
"""
from odoo.tests import HttpCase, new_test_user, tagged

#: What the steps of a check can use: wait for something, open a tab,
#: type into a field as a person does, read one back or check it is
#: not on screen, press Save and wait for its answer, read the
#: "Unsaved changes" hint, leave for another screen, and the panel's
#: requests from the moment it ran: those it made (``sent``), and some
#: it can hold back or refuse (``delayRpc``, ``failRpc``), through the
#: hook ``rpc`` keeps for tests.
#: Fields are found by their label among the rows on screen, since a
#: tab left behind keeps its rows, hidden.
_HELPERS = """
const sent = [];
const rpcStubs = {};
const { rpc } = odoo.loader.modules.get('@web/core/network/rpc');
const realRpc = rpc._rpc;
rpc._rpc = function (url, ...rest) {
    sent.push(url);
    const call = () => realRpc.call(this, url, ...rest);
    return rpcStubs[url] ? rpcStubs[url](call) : call();
};
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const delayRpc = (url, ...delays) => {
    let calls = 0;
    rpcStubs[url] = (call) => pause(
        delays[Math.min(calls++, delays.length - 1)],
    ).then(call);
};
const failRpc = (url) => {
    rpcStubs[url] = () => Promise.reject(new Error(`${url} refused`));
};
const until = async (what, check) => {
    for (let i = 0; i < 200; i++) {
        const found = check();
        if (found) return found;
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(`gave up waiting for ${what}`);
};
const tabButton = (name) => until(`the ${name} tab`, () => [
    ...document.querySelectorAll('button.rl-tab'),
].find((button) => button.textContent.trim() === name));
const openTab = async (name) => {
    const tab = await tabButton(name);
    tab.click();
    await until(`the ${name} tab to open`, () => tab.classList.contains('active'));
};
const shownRow = (label) => [...document.querySelectorAll('.rl-frow')]
    .find((row) => row.offsetParent !== null
        && row.querySelector(':scope > label')?.textContent.trim() === label);
const field = (label) => until(
    `the field "${label}"`, () => shownRow(label)?.querySelector('input'),
);
const expectHidden = (label) => {
    if (shownRow(label)) throw new Error(`"${label}" shows on another tab`);
};
const type = async (label, value) => {
    const input = await field(label);
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
};
const expectValue = async (label, value) => {
    const shown = (await field(label)).value;
    if (shown !== value) {
        throw new Error(`"${label}" shows ${shown}, not ${value}`);
    }
};
const pressSave = () => [...document.querySelectorAll('.rl-formfoot button')]
    .find((button) => button.textContent.trim() === 'Save changes')
    .click();
const saved = async () => {
    const said = await until('the save to answer', () => [
        ...document.querySelectorAll('.ic-toast-msg'),
    ].map((msg) => msg.textContent.trim())
        .find((text) => /^(Changes saved|Failed to save)/.test(text)));
    if (said !== 'Changes saved') throw new Error(said);
};
const save = async () => {
    pressSave();
    await saved();
};
const hint = () => document.querySelector('.rl-formfoot .rl-ff-hint');
const expectUnsaved = () => until('the unsaved changes hint', hint);
const expectAllSaved = () => until('the unsaved changes hint to go', () => !hint());
const leaveFor = (entry) => [...document.querySelectorAll('.ic-nav-label')]
    .find((label) => label.textContent.trim() === entry)
    .click();
const expectSent = (url, expected) => {
    if (sent.some((u) => u.endsWith(url)) !== expected) {
        throw new Error(`${url} ${expected ? 'never' : 'was'} sent`);
    }
};
"""


class SettingsInBrowser:
    """Drive the panel's Settings screen in a headless browser.

    A mixin, not a test case, so saas checks its own tabs the same way
    without collecting these tests twice. Mixed into an ``HttpCase``.
    """

    def _in_settings(self, tab, steps):
        """Open Settings on *tab* as an Administrator and run *steps*.

        :param str tab: the tab Settings opens on, e.g. ``rates``
        :param str steps: JavaScript run once the page is loaded, inside
            an async function with the helpers of ``_HELPERS`` in scope;
            it fails the test by throwing
        """
        login = 'settings-tabs-admin'
        new_test_user(
            self.env, login=login, password=login,
            groups='base.group_user,incubacloud.group_cloud_manager',
        )
        code = _HELPERS + """
            (async () => {
                try {
                    __STEPS__
                    console.log('test successful');
                } catch (error) {
                    console.error(error.message);
                }
            })();
        """.replace('__STEPS__', steps)
        self.browser_js(
            f'/cloud/settings?tab={tab}', code, login=login, timeout=120,
        )


@tagged('post_install', '-at_install')
class TestSettingsTabsKeepTheirEdits(SettingsInBrowser, HttpCase):
    """The core Rates tab, left and come back to, or saved from elsewhere."""

    _FIELD = 'GitHub webhook (per client IP)'

    def test_an_edit_survives_a_visit_to_another_tab(self):
        """Back on Rates, the field shows what was typed, not what is stored."""
        self._in_settings('rates', f"""
            await type('{self._FIELD}', '4321');
            await openTab('General');
            expectHidden('{self._FIELD}');
            await openTab('Rates');
            await expectValue('{self._FIELD}', '4321');
        """)

    def test_save_on_another_tab_stores_the_edit(self):
        """Save pressed on General sends what was typed on Rates."""
        settings = self.env['cloud.settings']._get()
        self.assertNotEqual(settings.rate_limit_webhook_per_min, 4321)
        self._in_settings('rates', f"""
            await type('{self._FIELD}', '4321');
            await openTab('General');
            await save();
            await expectAllSaved();
        """)
        self.env.invalidate_all()
        self.assertEqual(settings.rate_limit_webhook_per_min, 4321)

    def test_an_edit_on_rates_is_an_unsaved_change(self):
        """The footer says so, and leaving Settings asks first."""
        self._in_settings('rates', f"""
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
            leaveFor('Hosts');
            await until('the dialog asking before leaving', () => [
                ...document.querySelectorAll('.ic-confirm-box'),
            ].find((box) => box.textContent.includes('Unsaved changes')));
        """)

    def test_save_leaves_an_untouched_tab_alone(self):
        """Rates opened but not edited: Save does not send it again."""
        self._in_settings('rates', f"""
            await field('{self._FIELD}');
            await openTab('General');
            await save();
            expectSent('/cloud/save_general_settings', true);
            expectSent('/cloud/save_core_rate_limits', false);
        """)

    def test_an_edit_made_while_saving_stays_unsaved(self):
        """What is saved is what was sent, not what the field holds after."""
        settings = self.env['cloud.settings']._get()
        self._in_settings('rates', f"""
            delayRpc('/cloud/save_core_rate_limits', 1500);
            await type('{self._FIELD}', '4321');
            pressSave();
            await until('the rates save to go out', () => sent.some(
                (url) => url.endsWith('/cloud/save_core_rate_limits')));
            await type('{self._FIELD}', '4322');
            await saved();
            await expectUnsaved();
            await expectValue('{self._FIELD}', '4322');
        """)
        self.env.invalidate_all()
        self.assertEqual(settings.rate_limit_webhook_per_min, 4321)

    def test_a_tab_that_failed_to_load_shows_retry_not_its_form(self):
        """No form of defaults to edit, nothing sent; Retry loads it.

        Its form would hold defaults, not what is stored: the tab shows
        the error and Retry instead, and Save leaves it out. Once a retry
        loads it, it is a tab like the others.
        """
        settings = self.env['cloud.settings']._get()
        before = settings.rate_limit_webhook_per_min
        self._in_settings('general', f"""
            failRpc('/cloud/get_core_rate_limits');
            await openTab('Rates');
            const failed = await until('the load to fail', () => [
                ...document.querySelectorAll('.rl-stub[role=alert]'),
            ].find((box) => box.textContent.includes(
                'Failed to load rate-limit settings')));
            if (shownRow('{self._FIELD}')) {{
                throw new Error('a tab that failed to load shows its form');
            }}
            await save();
            expectSent('/cloud/save_core_rate_limits', false);
            delete rpcStubs['/cloud/get_core_rate_limits'];
            failed.querySelector('button').click();
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
        """)
        self.env.invalidate_all()
        self.assertEqual(settings.rate_limit_webhook_per_min, before)

    def test_a_tab_cancelled_while_loading_does_not_stand_in(self):
        """Settings draws again while Rates loads: the tab on screen counts.

        Rates is opened, and left before its first load answers, which
        draws Settings again: that tab is cancelled and another one takes
        its place. The first load answers last, and the cancelled tab
        must not be the one Settings compares and saves.
        """
        self._in_settings('general', f"""
            delayRpc('/cloud/get_core_rate_limits', 3000, 0);
            (await tabButton('Rates')).click();
            await pause(200);
            await openTab('General');
            await pause(3500);
            await openTab('Rates');
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
            await save();
            expectSent('/cloud/save_core_rate_limits', true);
        """)

    def test_a_retry_answered_after_its_tab_went_does_not_register(self):
        """A retry still out when Settings loads again does not register.

        "Check installation", on the GitHub App's second step, loads
        Settings again: every tab goes down and comes back as another.
        The retry of the tab that went answers last, and that tab must
        not stand in for the one now on screen.
        """
        app = self.env['cloud.github.app'].sudo().search([], limit=1)
        if app:
            app.installation_id = False
        else:
            self.env['cloud.github.app'].sudo().create({
                'app_id': '12345', 'private_key': 'test-key',
            })
        self._in_settings('general', f"""
            failRpc('/cloud/get_core_rate_limits');
            await openTab('Rates');
            const failed = await until('the load to fail', () => [
                ...document.querySelectorAll('.rl-stub[role=alert]'),
            ].find((box) => box.textContent.includes(
                'Failed to load rate-limit settings')));
            delayRpc('/cloud/get_core_rate_limits', 3000, 0);
            failed.querySelector('button').click();
            await pause(200);
            await openTab('GitHub');
            (await until('Check installation', () => [
                ...document.querySelectorAll('.rl-settings-body button'),
            ].find((button) => button.textContent.includes('Check installation'))))
                .click();
            await pause(3500);
            await openTab('Rates');
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
        """)
