"""Settings: one page per section, and a save bar that covers that page.

Settings is a menu of sections (/cloud/settings/<section>), each drawn by
the blocks registered for it, here and in the modules on top. A block has
its own form and joins the page's save bar once it loaded. The bar shows
only while something on the page is unsaved, counts it, checks every field
by its declaration before sending, sends only the blocks that changed and
puts a server error under its field. Leaving the page with unsaved changes
asks first, so no other section holds edits. What is saved is what was
sent: an edit made while the request is out stays unsaved. A block that
failed to load shows the error and Retry, not a form of defaults, and is
never sent; one that answers after the page moved on never stands in for
the one on screen.
"""
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.addons.incubacloud.tests._browser import ChromeStartRetry

#: What the steps of a check can use: wait for something, open a section,
#: type into a field as a person does, read one back or check it is not
#: on screen, press Save once the bar shows and wait for its answer, read
#: the bar, leave for another screen and answer the dialog that asks, and
#: the panel's requests from the moment it ran: those it made (``sent``,
#: with their parameters in ``calls``), and some it can hold back or
#: refuse (``delayRpc``, ``failRpc``, ``failRpcs`` for every url matching
#: a pattern), through the hook ``rpc`` keeps for tests.
#: Fields are found by their label among the rows on the page; one folded
#: under Advanced is unfolded first, as a person would.
_HELPERS = """
const sent = [];
const calls = [];
const rpcStubs = {};
const rpcRefused = [];
const { rpc } = odoo.loader.modules.get('@web/core/network/rpc');
const realRpc = rpc._rpc;
rpc._rpc = function (url, params, ...rest) {
    sent.push(url);
    calls.push({ url, params });
    const call = () => realRpc.call(this, url, params, ...rest);
    if (rpcRefused.some((pattern) => pattern.test(url))) {
        return Promise.reject(new Error(`${url} refused`));
    }
    return rpcStubs[url] ? rpcStubs[url](call) : call();
};
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const delayRpc = (url, ...delays) => {
    let count = 0;
    rpcStubs[url] = (call) => pause(
        delays[Math.min(count++, delays.length - 1)],
    ).then(call);
};
const failRpc = (url) => {
    rpcStubs[url] = () => Promise.reject(new Error(`${url} refused`));
};
const failRpcs = (pattern) => rpcRefused.push(pattern);
const until = async (what, check) => {
    for (let i = 0; i < 200; i++) {
        const found = check();
        if (found) return found;
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(`gave up waiting for ${what}`);
};
const menuItem = (name) => until(`the section ${name}`, () => [
    ...document.querySelectorAll('.ic-set-menu__item'),
].find((item) => item.textContent.trim() === name));
const openTab = async (name) => {
    const item = await menuItem(name);
    item.click();
    await until(`the section ${name} to open`,
        () => item.getAttribute('aria-current') === 'page'
            && document.querySelector('.ic-set-title')?.textContent.trim() === name);
};
const openSection = openTab;
const anyRow = (label) => [...document.querySelectorAll('.ic-set-page .ic-set-row')]
    .find((row) => row.querySelector('.ic-set-label')?.textContent.trim() === label);
const shownRow = (label) => {
    const row = anyRow(label);
    return row && row.offsetParent !== null ? row : null;
};
const control = (row) => document.getElementById(row.querySelector('.ic-set-label').htmlFor)
    || row.querySelector('input, textarea, select, button');
const field = async (label) => {
    const row = await until(`the field "${label}"`, () => anyRow(label));
    const fold = row.closest('.ic-set-adv');
    const toggle = fold?.querySelector('.ic-set-adv__toggle');
    if (toggle && toggle.getAttribute('aria-expanded') !== 'true') {
        toggle.click();
    }
    return until(`the field "${label}" to show`, () => shownRow(label) && control(shownRow(label)));
};
const expectHidden = (label) => {
    if (shownRow(label)) throw new Error(`"${label}" shows on this page`);
};
const type = async (label, value) => {
    let input = await field(label);
    if (input.tagName === 'BUTTON' && input.textContent.trim() === 'Replace') {
        input.click();
        input = await until(`"${label}" open for a new value`,
            () => shownRow(label).querySelector('input, textarea'));
    }
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur'));
    // As a person between two actions: the page draws what was typed
    // (a check passing again takes its error away) before the next one.
    await pause(150);
};
const expectValue = async (label, value) => {
    const shown = (await field(label)).value;
    if (shown !== value) {
        throw new Error(`"${label}" shows ${shown}, not ${value}`);
    }
};
const bar = () => document.querySelector('.ic-set-savebar');
const hint = bar;
const saveButton = () => [...document.querySelectorAll('.ic-set-savebar button')]
    .find((button) => button.textContent.trim() === 'Save changes');
const pressSave = async () => (await until('the Save button', saveButton)).click();
const saved = async () => {
    const said = await until('the save to answer', () => [
        ...document.querySelectorAll('.ic-toast-msg'),
    ].map((msg) => msg.textContent.trim()).find((text) => text === 'Saved')
        || document.querySelector('.ic-set-alert')?.textContent.trim()
        || document.querySelector('.ic-set-err')?.textContent.trim());
    if (said !== 'Saved') throw new Error(said);
};
const save = async () => {
    await pressSave();
    await saved();
};
const expectNoSave = () => {
    if (saveButton()) throw new Error('Save shows with nothing to save');
};
const expectUnsaved = () => until('the save bar', bar);
const expectAllSaved = () => until('the save bar to go', () => !bar());
const leaveFor = (entry) => [...document.querySelectorAll('.ic-nav-label')]
    .find((label) => label.textContent.trim() === entry)
    .click();
const leaveDialog = () => until('the dialog asking before leaving', () => [
    ...document.querySelectorAll('.ic-confirm-box'),
].find((box) => box.textContent.includes('Leave without saving?')));
const answerLeave = async (leave) => {
    const box = await leaveDialog();
    box.querySelector(leave ? '.ic-confirm-actions button:last-child'
        : '.ic-confirm-btn-cancel').click();
};
const expectSent = (url, expected) => {
    if (sent.some((u) => u.endsWith(url)) !== expected) {
        throw new Error(`${url} ${expected ? 'never' : 'was'} sent`);
    }
};
const lastParams = (url) => [...calls].reverse().find((c) => c.url.endsWith(url))?.params;
"""


class SettingsInBrowser(ChromeStartRetry):
    """Drive the panel's Settings screen in a headless browser.

    A mixin, not a test case, so a module on top checks its own sections the
    same way without collecting these tests twice. Mixed into an ``HttpCase``.
    """

    def _in_settings(self, section, steps, url=None):
        """Open Settings on *section* as an Administrator and run *steps*.

        :param str section: the section Settings opens on, e.g. ``rates``
        :param str steps: JavaScript run once the page is loaded, inside
            an async function with the helpers of ``_HELPERS`` in scope;
            it fails the test by throwing
        :param str url: another address to open instead, e.g. an old
            ``?tab=`` link
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
            url or f'/cloud/settings/{section}', code, login=login, timeout=120,
        )


@tagged('post_install', '-at_install')
class TestSettingsSections(SettingsInBrowser, HttpCase):
    """The core sections, edited, checked, saved and left."""

    _FIELD = 'GitHub webhook (per client IP)'

    def test_an_edit_is_unsaved_and_leaving_asks(self):
        """The bar shows; leaving the section or Settings asks, and staying keeps it."""
        self._in_settings('rates', f"""
            if (bar()) throw new Error('the bar shows with nothing changed');
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
            if (!bar().textContent.includes('1 setting')) {{
                throw new Error('the bar does not count the change: ' + bar().textContent);
            }}
            (await menuItem('General')).click();
            await answerLeave(false);
            await expectValue('{self._FIELD}', '4321');
            leaveFor('Hosts');
            await answerLeave(false);
            await expectValue('{self._FIELD}', '4321');
        """)

    def test_save_sends_the_section_only(self):
        """Logs & retention saves its fields; a General setting is not sent."""
        settings = self.env['cloud.settings']._get()
        self.env['ir.config_parameter'].sudo().set_param(
            'incubacloud.host_autoassign', '1',
        )
        self._in_settings('logs', """
            await type('Job retention', '45');
            await save();
            await expectAllSaved();
            const params = lastParams('/cloud/save_general_settings');
            if ('autoassign_enabled' in params || !('job_retention_days' in params)) {
                throw new Error('the save sent another section: ' + JSON.stringify(params));
            }
        """)
        self.env.invalidate_all()
        self.assertEqual(settings.job_retention_days, 45)
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param(
                'incubacloud.host_autoassign'),
            '1',
        )

    def test_discard_puts_the_page_back(self):
        """Discard returns every field to what was loaded, and the bar goes."""
        self._in_settings('instances', """
            const before = (await field('Keep the old copy')).value;
            await type('Keep the old copy', '7');
            await expectUnsaved();
            [...bar().querySelectorAll('button')]
                .find((b) => b.textContent.trim() === 'Discard').click();
            await expectAllSaved();
            await expectValue('Keep the old copy', before);
            expectSent('/cloud/save_general_settings', false);
        """)

    def test_a_wrong_value_stops_the_save_under_its_field(self):
        """Folded under Advanced: it opens, says how to fix it, nothing is sent."""
        self._in_settings('logs', """
            await type('Search timeout', '3');
            await pressSave();
            const error = await until('the error under the field', () =>
                shownRow('Search timeout')?.querySelector('.ic-set-err'));
            if (!error.textContent.includes('Must be 5 seconds or more.')) {
                throw new Error('the error says: ' + error.textContent);
            }
            if (!bar().textContent.includes('1 field needs attention')) {
                throw new Error('the bar says: ' + bar().textContent);
            }
            if ((await field('Search timeout')).getAttribute('aria-invalid') !== 'true') {
                throw new Error('the field is not marked invalid');
            }
            expectSent('/cloud/save_general_settings', false);
            await type('Search timeout', '20');
            await save();
            await expectAllSaved();
        """)
        settings = self.env['cloud.settings']._get()
        self.env.invalidate_all()
        self.assertEqual(settings.log_search_timeout_s, 20)

    def test_a_refusal_of_the_server_goes_under_its_field(self):
        """A range the model refuses is said under the ranges, and kept typed."""
        self._in_settings('edge', """
            await type('Trusted proxy ranges (override)', '2001:db8:::1/32');
            await pressSave();
            const error = await until('the error under the field', () =>
                shownRow('Trusted proxy ranges (override)')?.querySelector('.ic-set-err'));
            if (!error.textContent.trim()) throw new Error('the error is empty');
            await expectValue('Trusted proxy ranges (override)', '2001:db8:::1/32');
            await expectUnsaved();
        """)

    def test_an_edit_made_while_saving_stays_unsaved(self):
        """What is saved is what was sent, not what the field holds after."""
        settings = self.env['cloud.settings']._get()
        self._in_settings('rates', f"""
            delayRpc('/cloud/save_core_rate_limits', 1500);
            await type('{self._FIELD}', '4321');
            await pressSave();
            await until('the rates save to go out', () => sent.some(
                (url) => url.endsWith('/cloud/save_core_rate_limits')));
            await type('{self._FIELD}', '4322');
            await saved();
            await expectUnsaved();
            await expectValue('{self._FIELD}', '4322');
        """)
        self.env.invalidate_all()
        self.assertEqual(settings.rate_limit_webhook_per_min, 4321)

    def test_a_block_that_failed_to_load_shows_retry_not_its_form(self):
        """No form of defaults to edit, no bar; Retry loads it."""
        settings = self.env['cloud.settings']._get()
        before = settings.rate_limit_webhook_per_min
        self._in_settings('general', f"""
            failRpcs(/\\/get_(core_rate_limits|rate_limit_settings)$|\\/mcp\\/get_settings$/);
            await openTab('Rate limits');
            const failed = await until('the load to fail', () => [
                ...document.querySelectorAll('.rl-stub[role=alert]'),
            ].find((box) => box.textContent.includes(
                'Failed to load rate-limit settings')));
            if (shownRow('{self._FIELD}')) {{
                throw new Error('a block that failed to load shows its form');
            }}
            expectNoSave();
            rpcRefused.length = 0;
            failed.querySelector('button').click();
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
        """)
        self.env.invalidate_all()
        self.assertEqual(settings.rate_limit_webhook_per_min, before)

    def test_a_block_answered_after_its_page_went_does_not_stand_in(self):
        """Rate limits left before it loads: back on it, the block on screen counts."""
        self._in_settings('general', f"""
            delayRpc('/cloud/get_core_rate_limits', 3000, 0);
            (await menuItem('Rate limits')).click();
            await pause(200);
            await openTab('General');
            await pause(3500);
            await openTab('Rate limits');
            await type('{self._FIELD}', '4321');
            await expectUnsaved();
            await save();
            expectSent('/cloud/save_core_rate_limits', true);
        """)

    def test_the_search_leads_to_the_field(self):
        """A field folded under Advanced: its section opens on it, marked."""
        self._in_settings('general', """
            const box = await until('the search box', () => [
                ...document.querySelectorAll('.ic-set-search input'),
            ].find((input) => input.offsetParent !== null));
            box.focus();
            box.value = 'timeout';
            box.dispatchEvent(new Event('input', { bubbles: true }));
            const result = await until('the result', () => [
                ...document.querySelectorAll('.ic-set-result'),
            ].find((r) => r.textContent.includes('Search timeout')));
            result.click();
            await until('the section to open', () =>
                location.pathname === '/cloud/settings/logs'
                && location.hash === '#log_search_timeout_s');
            const row = await until('the field marked', () =>
                document.querySelector('#ic-set-log_search_timeout_s.ic-set-row--found'));
            if (row.offsetParent === null) throw new Error('its Advanced stayed folded');
            await until('Back to results', () => [
                ...document.querySelectorAll('.ic-set-resultfor button'),
            ].find((b) => b.textContent.includes('Back to results')));
        """)

    def test_a_link_to_an_old_tab_opens_its_section(self):
        """Bookmarks and docs that name a tab land on the section it became."""
        self._in_settings('', """
            await until('Rate limits open', () =>
                document.querySelector('.ic-set-title')?.textContent.trim() === 'Rate limits');
        """, url='/cloud/settings?tab=rates')
