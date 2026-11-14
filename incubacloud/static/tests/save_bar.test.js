import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, useState, xml } from "@odoo/owl";
import { mountWithCleanup, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import {
    restoreForm, SaveBar, unsavedCount,
} from "@incubacloud/components/settings_form/save_bar";

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
            <input class="name" t-att-value="state.form.name"/>
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
