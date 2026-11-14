/** @odoo-module **/

/**
 * The save bar (ESTANDAR-FORMULARIOS §7): it floats at the bottom of the
 * page while something on it is unsaved, says how much, and holds Discard
 * and Save changes. ``Ctrl/⌘ S`` saves while it is on the page, and not
 * under a dialog.
 *
 * Settings and the records' pages in edit mode share it. The page keeps
 * its form and what was last saved; the bar only shows the count and calls
 * back. A page with no changes and no errors shows no bar.
 */

import { Component, onMounted, onWillUnmount, status } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * How many fields of a form differ from what was last saved.
 *
 * @param {object} form the values as they are on screen
 * @param {object|null} saved the same values as last saved; without it,
 *     nothing counts as changed
 * @returns {number} the fields that differ, in either direction
 */
export function unsavedCount(form, saved) {
    if (!saved) {
        return 0;
    }
    const keys = new Set([...Object.keys(form || {}), ...Object.keys(saved)]);
    let count = 0;
    for (const key of keys) {
        if (JSON.stringify(form?.[key]) !== JSON.stringify(saved[key])) {
            count += 1;
        }
    }
    return count;
}

/**
 * Put a form back as it was last saved, in place, so what reads it keeps
 * the same object.
 *
 * @param {object} form the form to put back
 * @param {object} saved the values as last saved
 * @param {string[]} [keep] fields of the form the snapshot leaves out on
 *     purpose (text typed but not added yet), left as they are
 */
export function restoreForm(form, saved, keep = []) {
    for (const key of Object.keys(form)) {
        if (!(key in saved) && !keep.includes(key)) {
            form[key] = undefined;
        }
    }
    for (const [key, value] of Object.entries(saved)) {
        if (JSON.stringify(form[key]) !== JSON.stringify(value)) {
            form[key] = value === undefined ? undefined : JSON.parse(JSON.stringify(value));
        }
    }
}

/**
 * Hand the value of the text field being typed in to its form, as leaving
 * the field would: a ``change`` on it, which a field that takes its value
 * on change listens for. The focus stays where it is.
 *
 * @returns {boolean} whether a field was focused and told
 */
function commitFocusedField() {
    const el = document.activeElement;
    if (!el?.matches?.("input, textarea")) {
        return false;
    }
    if (["button", "checkbox", "file", "radio", "reset", "submit"].includes(el.type)) {
        return false;
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
}

export class SaveBar extends Component {
    static template = "incubacloud.SaveBar";
    static props = {
        // How many settings or fields are unsaved; 0 hides the bar.
        count: Number,
        // How many fields show an error; the bar stays to say so.
        errors: { type: Number, optional: true },
        // A save is out: both buttons wait.
        saving: { type: Boolean, optional: true },
        // False while the form cannot be saved as it is (a required field
        // left empty): Save changes waits, Discard does not.
        canSave: { type: Boolean, optional: true },
        onSave: Function,
        onDiscard: Function,
        // Keep room under the page's content while the bar shows, so the
        // bar never covers its last lines.
        reserveSpace: { type: Boolean, optional: true },
    };

    /** Listen for ``Ctrl/⌘ S`` while the bar is on the page. */
    setup() {
        this._onKeydown = (ev) => this.onKeydown(ev);
        onMounted(() => window.addEventListener("keydown", this._onKeydown));
        onWillUnmount(() => window.removeEventListener("keydown", this._onKeydown));
    }

    /** @returns {boolean} whether the bar shows: something unsaved or wrong */
    get shown() {
        return this.props.count > 0 || (this.props.errors || 0) > 0;
    }

    /** @returns {boolean} whether Save changes can run now */
    get saveDisabled() {
        return Boolean(this.props.saving) || this.props.canSave === false;
    }

    /** @returns {string} what the bar says */
    get text() {
        const errors = this.props.errors || 0;
        if (errors) {
            return errors === 1
                ? _t("1 field needs attention")
                : _t("%s fields need attention", errors);
        }
        return _t("Unsaved changes");
    }

    /** @returns {string} how many settings the bar would save */
    get countText() {
        const n = this.props.count;
        return n === 1 ? _t("1 setting") : _t("%s settings", n);
    }

    /**
     * ``Ctrl/⌘ S`` saves, when there is something to save; the browser's
     * own save never opens over the page. The field being typed in hands
     * its value to the form first, as leaving it would, so a field that
     * takes its value on change is saved with what it shows. With a dialog
     * open, the page under it is not saved.
     *
     * @param {KeyboardEvent} ev
     * @returns {Promise<void>} once the save, if any, has been asked for
     */
    async onKeydown(ev) {
        if (!(ev.ctrlKey || ev.metaKey) || (ev.key !== "s" && ev.key !== "S")) {
            return;
        }
        ev.preventDefault();
        if (document.querySelector('[aria-modal="true"], .modal[role="dialog"]')) {
            return;
        }
        if (commitFocusedField()) {
            // The page renders the value it took, and the count with it,
            // before the next task.
            await new Promise((resolve) => setTimeout(resolve, 0));
            if (status(this) === "destroyed") {
                return;
            }
        }
        if (this.props.count > 0 && !this.saveDisabled) {
            this.props.onSave();
        }
    }
}
