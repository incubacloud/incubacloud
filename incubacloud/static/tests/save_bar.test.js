import { animationFrame, beforeEach, describe, expect, test, tick } from "@odoo/hoot";
import { click, edit, queryOne } from "@odoo/hoot-dom";
import { Component, useState, xml } from "@odoo/owl";
import { allowTranslations, mountWithCleanup, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import {
    restoreForm, SaveBar, unsavedCount,
} from "@incubacloud/components/settings_form/save_bar";
import { PasswordInput } from "@incubacloud/components/password_input/password_input";
import { relayIcon } from "@incubacloud/utils/icons";

/**
 * SaveBar: the bar Settings and the records' pages share. It shows only
 * with something unsaved, counts it, puts the form back on Discard and
 * saves on Save changes or Ctrl/⌘ S. Mounted for real under a page that
 * keeps a form and what was last saved, as the records' pages do.
 */

/** The page on screen, to reach its form and its calls. */
let page = null;

/** A page with a form, a snapshot of it and the bar in edit mode. */
class Page extends Component {
    static template = xml`
        <div>
            <input class="name" t-att-value="state.form.name"
                   t-on-change="(ev) => { state.form.name = ev.target.value; }"/>
            <div t-if="state.dialog" class="dialog" role="dialog" aria-modal="true">
                <input class="in-dialog"/>
            </div>
            <SaveBar count="count" saving="state.saving" canSave="state.canSave"
                     onSave.bind="save" onDiscard.bind="discard" reserveSpace="true"/>
        </div>`;
    static components = { SaveBar };
    static props = {};

    /** Keep a form and what was saved; count the saves. */
    setup() {
        this.state = useState({
            form: { name: "web", port: 8069, tags: [1] },
            saving: false,
            canSave: true,
            dialog: false,
        });
        this.saved = JSON.parse(JSON.stringify(this.state.form));
        this.saves = 0;
        page = this;
    }

    /** @returns {number} the fields that differ from what was saved */
    get count() {
        return unsavedCount(this.state.form, this.saved);
    }

    /** Take the form as saved. */
    save() {
        this.saves += 1;
        this.saved = JSON.parse(JSON.stringify(this.state.form));
        this.render();
    }

    /** Put the form back as saved. */
    discard() {
        restoreForm(this.state.form, this.saved);
    }
}

const bar = () => document.querySelector(".ic-set-savebar");
const button = (label) => [...document.querySelectorAll(".ic-set-savebar button")]
    .find((b) => b.textContent.trim() === label);

/**
 * Press Ctrl S, or ⌘ S, on the window.
 *
 * @param {object} [mods] the modifier held
 * @returns {KeyboardEvent} the event, to tell whether it was taken
 */
function pressSave(mods = { ctrlKey: true }) {
    const ev = new KeyboardEvent("keydown", { key: "s", cancelable: true, ...mods });
    window.dispatchEvent(ev);
    return ev;
}

describe("SaveBar — what it counts", () => {
    test("the fields that differ, either way", () => {
        expect(unsavedCount({ a: 1, b: [1] }, { a: 1, b: [1] })).toBe(0);
        expect(unsavedCount({ a: 2, b: [1, 2] }, { a: 1, b: [1] })).toBe(2);
        expect(unsavedCount({ a: 1, c: "x" }, { a: 1 })).toBe(1);
        expect(unsavedCount({ a: 1 }, { a: 1, d: true })).toBe(1);
    });

    test("nothing before a snapshot", () => {
        expect(unsavedCount({ a: 1 }, null)).toBe(0);
    });

    test("restoring puts the saved values back in the same form", () => {
        const form = { a: 2, b: [1, 2], typed: "x", extra: 1 };
        const saved = { a: 1, b: [1] };
        restoreForm(form, saved, ["typed"]);
        expect(form).toEqual({ a: 1, b: [1], typed: "x", extra: undefined });
        // A copy: editing the form again leaves the snapshot alone.
        form.b.push(3);
        expect(saved.b).toEqual([1]);
    });
});

describe("SaveBar — on the page", () => {
    // The bar asks the server nothing, and the mail stack of the test env
    // must not ask it either (as in ic_menu.test.js).
    beforeEach(() => patchWithCleanup(rpc, { _rpc: () => Promise.resolve(false) }));

    test("no changes, no bar", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        expect(bar()).toBe(null);
        expect(document.querySelector(".ic-set-savebar-room")).toBe(null);
    });

    test("an edit shows it with the count", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        page.state.form.name = "web2";
        await animationFrame();
        expect(bar()).not.toBe(null);
        expect(bar().textContent).toInclude("Unsaved changes");
        expect(bar().textContent).toInclude("1 setting");
        expect(document.querySelector(".ic-set-savebar-room")).not.toBe(null);
        page.state.form.port = 8070;
        await animationFrame();
        expect(bar().textContent).toInclude("2 settings");
    });

    test("Discard puts the form back as saved, and the bar goes", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        page.state.form.name = "web2";
        page.state.form.tags.push(2);
        await animationFrame();
        await click(button("Discard"));
        await animationFrame();
        expect(page.state.form).toEqual({ name: "web", port: 8069, tags: [1] });
        expect(bar()).toBe(null);
        expect(page.saves).toBe(0);
    });

    test("Save changes saves, and the bar goes", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        page.state.form.name = "web2";
        await animationFrame();
        await click(button("Save changes"));
        await animationFrame();
        expect(page.saves).toBe(1);
        expect(bar()).toBe(null);
    });

    test("Ctrl S and ⌘ S save only with something to save", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        // Nothing unsaved: taken from the browser, nothing sent.
        expect(pressSave().defaultPrevented).toBe(true);
        expect(page.saves).toBe(0);
        page.state.form.name = "web2";
        await animationFrame();
        pressSave();
        expect(page.saves).toBe(1);
        page.state.form.port = 1;
        await animationFrame();
        pressSave({ metaKey: true });
        expect(page.saves).toBe(2);
        // Another key with Ctrl is left alone.
        const other = new KeyboardEvent("keydown", { key: "p", ctrlKey: true, cancelable: true });
        window.dispatchEvent(other);
        expect(other.defaultPrevented).toBe(false);
    });

    test("while saving, or with the form not ready, Save waits", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        page.state.form.name = "";
        page.state.canSave = false;
        await animationFrame();
        expect(button("Save changes").disabled).toBe(true);
        expect(button("Discard").disabled).toBe(false);
        pressSave();
        expect(page.saves).toBe(0);
        page.state.canSave = true;
        page.state.saving = true;
        await animationFrame();
        expect(button("Discard").disabled).toBe(true);
        expect(bar().textContent).toInclude("Saving…");
        pressSave();
        expect(page.saves).toBe(0);
    });

    test("Ctrl S takes the value of the field being typed in, which a change hands over", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        const input = queryOne("input.name");
        input.focus();
        // Typed, not left yet: the form has not had the value.
        input.value = "web2";
        expect(page.state.form.name).toBe("web");
        pressSave();
        await tick();
        expect(page.saves).toBe(1);
        expect(page.saved.name).toBe("web2");
        // The focus stays in the field.
        expect(input).toBeFocused();
    });

    test("with a dialog open, Ctrl S leaves the page under it alone", async () => {
        await mountWithCleanup(Page, { noMainContainer: true });
        page.state.form.port = 1;
        page.state.dialog = true;
        await animationFrame();
        queryOne("input.in-dialog").focus();
        expect(pressSave().defaultPrevented).toBe(true);
        await tick();
        expect(page.saves).toBe(0);
        page.state.dialog = false;
        await animationFrame();
        pressSave();
        expect(page.saves).toBe(1);
    });

    test("it stays to say a field needs attention", async () => {
        class Errors extends Component {
            static template = xml`<SaveBar count="0" errors="2" onSave="() => {}" onDiscard="() => {}"/>`;
            static components = { SaveBar };
            static props = {};
        }
        await mountWithCleanup(Errors, { noMainContainer: true });
        expect(bar().textContent).toInclude("2 fields need attention");
        expect(document.querySelector(".ic-set-savebar__dot--err")).not.toBe(null);
    });
});

/** The page with a password field on screen, to reach its form. */
let secretPage = null;

/**
 * A record's page with a stored password, as an instance's, a host's and a
 * backup destination's are: the form holds "" for it until a new one is
 * typed, and the field shows what the form holds.
 */
class SecretPage extends Component {
    static template = xml`
        <div>
            <PasswordInput id="'pw'" value="state.form.secret" hasValue="props.stored"
                           onChange="(value) => { state.form.secret = value; }"/>
            <SaveBar count="count" onSave="() => {}" onDiscard.bind="discard"/>
        </div>`;
    static components = { PasswordInput, SaveBar };
    static props = { stored: Boolean };

    /** Keep the form and what was saved. */
    setup() {
        this.state = useState({ form: { secret: "" } });
        this.saved = { secret: "" };
        secretPage = this;
    }

    /** @returns {number} the fields that differ from what was saved */
    get count() {
        return unsavedCount(this.state.form, this.saved);
    }

    /** Put the form back as saved. */
    discard() {
        restoreForm(this.state.form, this.saved);
    }
}

describe("SaveBar — Discard and a password field", () => {
    beforeEach(() => {
        // The field's placeholder is translated, read here as text.
        allowTranslations();
        patchWithCleanup(rpc, { _rpc: () => Promise.resolve(false) });
    });

    /**
     * Mount the page with its password stored or not.
     *
     * @param {boolean} stored whether a password is stored
     */
    async function mountSecret(stored) {
        await mountWithCleanup(SecretPage, {
            props: { stored },
            env: { icon: relayIcon },
            noMainContainer: true,
        });
    }

    test("a stored one replaced, then discarded, reads Stored again", async () => {
        await mountSecret(true);
        await click(".ic-password-stored button");
        await animationFrame();
        await click("input#pw");
        await edit("typed-then-discarded");
        await animationFrame();
        expect(secretPage.state.form.secret).toBe("typed-then-discarded");
        await click(button("Discard"));
        await animationFrame();
        expect(secretPage.state.form.secret).toBe("");
        expect(".ic-password-stored").toHaveText(/Stored/);
        expect("input#pw").toHaveCount(0);
        expect(bar()).toBe(null);
    });

    test("one not stored, typed, then discarded, is an empty input again", async () => {
        await mountSecret(false);
        await click("input#pw");
        await edit("typed-then-discarded");
        await animationFrame();
        await click(button("Discard"));
        await animationFrame();
        expect(".ic-password-stored").toHaveCount(0);
        expect("input#pw").toHaveValue("");
        expect("input#pw").toHaveAttribute("placeholder", "Not set");
        expect(".ic-password-hint").toHaveCount(0);
    });
});
