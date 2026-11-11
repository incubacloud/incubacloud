/** @odoo-module **/

/**
 * Settings: a menu of sections, one page each (/cloud/settings/<section>).
 *
 * The menu, grouped, with "Search settings" atop it; on a phone, a select of
 * the section with the search under it. The page shows the blocks registered
 * for its section (settings_registry.js), and a save bar that floats at the
 * bottom while something on the page is unsaved. The bar covers the page
 * only: leaving it with unsaved changes asks first, so another section never
 * holds edits of its own.
 *
 * Each block keeps its own form and joins the bar through
 * ``env.registerSettingsSave(key, save, state, writeOnly)`` once it loaded:
 * the bar counts the fields of its form that differ from what was loaded or
 * last saved, checks them by their declarations before saving, sends only
 * the blocks that changed, and puts a server error under its field when the
 * answer names one. ``writeOnly`` are fields the server keeps and never
 * shows back (secrets), empty again once saved.
 */

import {
    Component, onMounted, onPatched, onWillUnmount, onWillUpdateProps, reactive, useState,
    useSubEnv, batched,
} from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { useNavGuard } from "../../utils/use_nav_guard";
import { confirmVia } from "../../utils/use_confirm";
import {
    blocksOf, fieldProblem, sectionFor, settingField, settingsMenu,
} from "./settings_registry";
import { SettingsSearch } from "./settings_search_box";

/** How long the field the search led to stays marked, in ms. */
export const FOUND_MS = 2500;

/**
 * The fields of a form that differ from a snapshot of it.
 *
 * @param {object} form the form as it is
 * @param {Object<string, string>} saved each field's JSON when last saved
 * @returns {string[]} the keys that changed
 */
export function changedKeys(form, saved) {
    return Object.keys(form).filter(
        (key) => JSON.stringify(form[key]) !== (saved || {})[key],
    );
}

/**
 * A snapshot of a form: each field as JSON, to compare with later.
 *
 * @param {object} form the form
 * @returns {Object<string, string>} the snapshot
 */
export function snapshot(form) {
    return Object.fromEntries(
        Object.entries(form).map(([key, value]) => [key, JSON.stringify(value)]),
    );
}

export class Settings extends Component {
    static template = "incubacloud.Settings";
    static components = { IcConfirmDialog, SettingsSearch };
    static props = {
        // The section the URL names; General when none.
        section: { type: String, optional: true },
        // The field the URL's #<field> points at.
        field: { type: String, optional: true },
        // The ?tab= of a link from before the sections.
        tab: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            saving: false,
            // An error of the save that belongs to no field, atop the page.
            pageError: null,
            // What the search was for when it led here ("Back to results").
            resultFor: null,
            confirmDialog: null,
        });
        // Shared with the rows: what was saved, what is wrong, what the
        // search found. Rows subscribe to it with useState.
        this.store = useState(reactive({ saved: {}, errors: {}, found: null }));
        this._tabs = new Map();
        const rerender = batched(() => this.render());
        useSubEnv({
            registerSettingsSave: (key, save, state, writeOnly = []) => {
                const tab = { save, writeOnly, state: state && reactive(state, rerender) };
                this._tabs.set(key, tab);
                if (tab.state) {
                    this.store.saved[key] = snapshot(tab.state.form);
                }
                rerender();
            },
            unregisterSettingsSave: (key) => {
                this._tabs.delete(key);
                delete this.store.saved[key];
                rerender();
            },
            settingsForm: {
                store: this.store,
                isChanged: (id, store) => this.isChanged(id, store),
                setError: (id, message) => this.setError(id, message),
            },
        });

        this._pendingField = this.props.field || null;
        this._foundTimer = null;
        onWillUpdateProps((nextProps) => this._onWillUpdateProps(nextProps));
        onMounted(() => this._showPendingField());
        onPatched(() => this._showPendingField());
        onWillUnmount(() => clearTimeout(this._foundTimer));

        this._onKeydown = (ev) => this.onKeydown(ev);
        onMounted(() => window.addEventListener("keydown", this._onKeydown));
        onWillUnmount(() => window.removeEventListener("keydown", this._onKeydown));

        useNavGuard(
            () => this.isDirty,
            (opts) => this._confirm({
                ...opts,
                title: _t("Leave without saving?"),
                message: _t("The changes on this page are not saved. Leave it and lose them?"),
                confirmLabel: _t("Leave without saving"),
            }),
        );
    }

    /**
     * Remember a field the URL now points at, and drop the errors of a
     * section left behind.
     *
     * @param {object} nextProps the props the page is about to get
     */
    _onWillUpdateProps(nextProps) {
        if (nextProps.field && nextProps.field !== this.props.field) {
            this._pendingField = nextProps.field;
        }
        if (nextProps.section !== this.props.section) {
            this.store.errors = {};
            this.state.pageError = null;
        }
    }

    // ── What is on screen ─────────────────────────────────────────────

    /** @returns {string} the id of the section on screen */
    get sectionId() {
        const id = sectionFor(this.props.section, this.props.tab);
        const all = this.menu.flatMap((group) => group.sections.map((s) => s.id));
        return all.includes(id) ? id : (all[0] || id);
    }

    /** @returns {object[]} the groups of the menu, with their sections */
    get menu() {
        return settingsMenu(this.env);
    }

    /** @returns {object|null} the section on screen, with its group's title */
    get section() {
        for (const group of this.menu) {
            const section = group.sections.find((s) => s.id === this.sectionId);
            if (section) {
                return { ...section, groupTitle: group.title };
            }
        }
        return null;
    }

    /** @returns {string} the line that names what the search was for */
    get resultForText() {
        return _t("Result for “%s”.", this.state.resultFor || "");
    }

    /** @returns {object[]} the blocks of the section on screen */
    get blocks() {
        return blocksOf(this.sectionId, this.env);
    }

    // ── Unsaved changes ───────────────────────────────────────────────

    /**
     * Whether a field differs from what was loaded or last saved.
     *
     * @param {string} id the field's id
     * @param {object} [store] the store as the caller reads it, so the
     *     caller re-renders when a save moves the snapshot
     * @returns {boolean}
     */
    isChanged(id, store = this.store) {
        for (const [key, tab] of this._tabs) {
            if (tab.state && id in tab.state.form) {
                return JSON.stringify(tab.state.form[id]) !== (store.saved[key] || {})[id];
            }
        }
        return false;
    }

    /** @returns {number} how many settings on the page are unsaved */
    get changedCount() {
        let count = 0;
        for (const [key, tab] of this._tabs) {
            if (tab.state) {
                count += changedKeys(tab.state.form, this.store.saved[key]).length;
            }
        }
        return count;
    }

    /** @returns {boolean} whether anything on the page is unsaved */
    get isDirty() {
        return this.changedCount > 0;
    }

    /** @returns {number} how many fields show an error */
    get errorCount() {
        return Object.values(this.store.errors).filter(Boolean).length;
    }

    /** @returns {string} what the save bar says */
    get barText() {
        const errors = this.errorCount;
        if (errors) {
            return errors === 1
                ? _t("1 field needs attention")
                : _t("%s fields need attention", errors);
        }
        return _t("Unsaved changes");
    }

    /** @returns {string} how many settings the bar would save */
    get barCount() {
        const n = this.changedCount;
        return n === 1 ? _t("1 setting") : _t("%s settings", n);
    }

    /**
     * Show, or clear, what is wrong with a field.
     *
     * @param {string} id the field's id
     * @param {string|null} message how to fix it, or null when it is fine
     */
    setError(id, message) {
        if (message) {
            this.store.errors[id] = message;
        } else {
            delete this.store.errors[id];
        }
    }

    /**
     * Check every field the page's forms hold that Settings declares.
     *
     * @returns {Object<string, string>} the problems, by field id
     */
    _problems() {
        const problems = {};
        for (const [, tab] of this._tabs) {
            if (!tab.state) {
                continue;
            }
            for (const [id, value] of Object.entries(tab.state.form)) {
                const field = settingField(id);
                const problem = field && fieldProblem(field, value, tab.state.form);
                if (problem) {
                    problems[id] = problem;
                }
            }
        }
        return problems;
    }

    /** Put the focus on the first field in error, in the page's order. */
    _focusFirstError() {
        // After the render that opened an Advanced holding it.
        setTimeout(() => {
            const row = [...document.querySelectorAll(".ic-set-page .ic-set-row")]
                .find((el) => this.store.errors[el.dataset.field]);
            row?.querySelector("input, textarea, button, [tabindex]")?.focus();
        });
    }

    /**
     * Save the page: check it, then send each block that changed.
     *
     * What is sent, not what the form holds once the answers are in, is
     * what counts as saved: an edit made while a request is out stays
     * unsaved.
     */
    async save() {
        if (this.state.saving) {
            return;
        }
        const problems = this._problems();
        this.store.errors = problems;
        if (Object.keys(problems).length) {
            this._focusFirstError();
            return;
        }
        this.state.saving = true;
        this.state.pageError = null;
        const failures = [];
        for (const [key, tab] of [...this._tabs]) {
            if (tab.state && !changedKeys(tab.state.form, this.store.saved[key]).length) {
                continue;
            }
            const sent = tab.state && { ...tab.state.form };
            try {
                const result = await tab.save();
                if (result && result.ok === false) {
                    if (result.field && settingField(result.field)) {
                        this.setError(result.field, result.error || _t("Not saved."));
                    } else {
                        failures.push(result.error || _t("Not saved."));
                    }
                    continue;
                }
                if (sent && this._tabs.get(key) === tab) {
                    for (const field of tab.writeOnly) {
                        sent[field] = "";
                    }
                    this.store.saved[key] = snapshot(sent);
                }
            } catch (error) {
                failures.push(error?.data?.message || error?.message || _t("Not saved."));
            }
        }
        this.state.saving = false;
        if (failures.length) {
            this.state.pageError = failures.join(" ");
        }
        if (this.errorCount) {
            this._focusFirstError();
        }
        if (!failures.length && !this.errorCount) {
            this.env.toast?.success(_t("Saved"));
        }
    }

    /** Put every block back as it was loaded or last saved. */
    discard() {
        for (const [key, tab] of this._tabs) {
            const saved = this.store.saved[key];
            if (tab.state && saved) {
                for (const field of changedKeys(tab.state.form, saved)) {
                    tab.state.form[field] = field in saved ? JSON.parse(saved[field]) : undefined;
                }
            }
        }
        this.store.errors = {};
        this.state.pageError = null;
    }

    // ── Moving around ─────────────────────────────────────────────────

    /**
     * Open a section; the page asks first when something is unsaved.
     *
     * @param {string} sectionId the section's id
     */
    openSection(sectionId) {
        this.state.resultFor = null;
        this.env.navigate("settings", { section: sectionId });
    }

    /**
     * The phone's select changed: open that section, or put the select
     * back when leaving was refused.
     *
     * @param {Event} ev the select's change event
     */
    async onSelectSection(ev) {
        const target = ev.target;
        await this.env.navigate("settings", { section: target.value });
        target.value = this.sectionId;
    }

    /**
     * The search picked a field: open its section on it.
     *
     * @param {object} field the index entry
     * @param {string} query what was typed, for "Back to results"
     */
    async onPickField(field, query) {
        this.state.resultFor = query;
        this._pendingField = field.id;
        await this.env.navigate("settings", { section: field.section, field: field.id });
        this._showPendingField();
    }

    /**
     * The search picked a page outside Settings.
     *
     * @param {object} entry the elsewhere entry
     */
    onPickElsewhere(entry) {
        this.env.navigate(entry.route, entry.params || {});
    }

    /** Open the search again on what led here. */
    backToResults() {
        const query = this.state.resultFor;
        this.state.resultFor = null;
        const box = [...document.querySelectorAll(".ic-set-search input")]
            .find((input) => input.offsetParent !== null);
        if (box) {
            box.value = query;
            box.dispatchEvent(new Event("input", { bubbles: true }));
            box.focus();
        }
    }

    /**
     * Mark, centre and focus the field the URL or the search points at,
     * once its row is on screen.
     */
    _showPendingField() {
        const id = this._pendingField;
        if (!id || !document.getElementById(`ic-set-${id}`)) {
            return;
        }
        this._pendingField = null;
        this.store.found = id;
        clearTimeout(this._foundTimer);
        // After the render that opened an Advanced holding it.
        setTimeout(() => {
            const row = document.getElementById(`ic-set-${id}`);
            if (!row) {
                return;
            }
            row.scrollIntoView?.({ block: "center" });
            row.querySelector("input, textarea, button")?.focus({ preventScroll: true });
        });
        this._foundTimer = setTimeout(() => {
            if (this.store.found === id) {
                this.store.found = null;
            }
        }, FOUND_MS);
    }

    /**
     * ``/`` focuses the search unless typing in a field; ``Ctrl/⌘ S``
     * saves the page.
     *
     * @param {KeyboardEvent} ev
     */
    onKeydown(ev) {
        if ((ev.ctrlKey || ev.metaKey) && (ev.key === "s" || ev.key === "S")) {
            ev.preventDefault();
            if (this.isDirty) {
                this.save();
            }
            return;
        }
        if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) {
            return;
        }
        const target = ev.target;
        if (target?.closest?.("input, textarea, select, [contenteditable='true']")) {
            return;
        }
        const box = [...document.querySelectorAll(".ic-set-search input")]
            .find((input) => input.offsetParent !== null);
        if (box) {
            ev.preventDefault();
            box.focus();
        }
    }

    /** Open the shared confirmation dialog. See utils/use_confirm.js. */
    _confirm(opts) {
        return confirmVia(this.state, opts);
    }
}
