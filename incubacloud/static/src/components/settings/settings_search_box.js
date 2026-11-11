/** @odoo-module **/

import { Component, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { elsewhereEntries, settingsIndex } from "./settings_registry";
import {
    MIN_QUERY, markMatch, searchElsewhere, searchSettings, suggestion,
} from "./settings_search";

let nextId = 0;

/**
 * "Search settings": the box atop the Settings menu and its results.
 *
 * Results come grouped by section in the order of the menu, three each and
 * "+N more in …", then "Elsewhere in the panel". ``↑`` ``↓`` move, Enter
 * opens, Esc closes. With nothing found it offers the closest word of the
 * index and the panel's Ctrl K.
 */
export class SettingsSearch extends Component {
    static template = "incubacloud.SettingsSearch";
    static props = {
        onPickField: Function,
        onPickElsewhere: Function,
    };

    /** An empty, closed box. */
    setup() {
        this.uid = `ic-set-search-${++nextId}`;
        this.input = useRef("input");
        this.state = useState({ query: "", open: false, active: 0 });
    }

    /** @returns {boolean} whether there is enough to search */
    get searching() {
        return this.state.query.trim().length >= MIN_QUERY;
    }

    /** @returns {{total: number, groups: object[]}} the matching settings */
    get results() {
        return searchSettings(settingsIndex(this.env), this.state.query);
    }

    /** @returns {object[]} the matching pages outside Settings */
    get elsewhere() {
        return searchElsewhere(elsewhereEntries(this.env), this.state.query);
    }

    /** @returns {object[]} what ↑ and ↓ move through, in order */
    get items() {
        const items = [];
        for (const group of this.results.groups) {
            for (const field of group.fields) {
                items.push({ kind: "field", key: `f-${field.id}`, field });
            }
        }
        for (const entry of this.elsewhere) {
            items.push({ kind: "elsewhere", key: `e-${entry.key}`, entry });
        }
        return items;
    }

    /** @returns {string|null} the closest word, when nothing matched */
    get didYouMean() {
        if (this.items.length) {
            return null;
        }
        return suggestion(settingsIndex(this.env), this.state.query);
    }

    /** @returns {string} what the results say when nothing matched */
    get noMatchText() {
        return _t("No settings match “%s”.", this.state.query.trim());
    }

    /** @returns {string} the count the results announce */
    get countLabel() {
        const n = this.results.total;
        return n === 1 ? _t("1 result") : _t("%s results", n);
    }

    /**
     * The parts of a label around the match, to mark it.
     *
     * @param {string} label
     * @returns {{before: string, match: string, after: string}}
     */
    mark(label) {
        return markMatch(label, this.state.query);
    }

    /**
     * Whether an item is the one ↑ ↓ stand on.
     *
     * @param {string} key the item's key
     * @returns {boolean}
     */
    isActive(key) {
        return this.items[this.state.active]?.key === key;
    }

    /**
     * Search as one types.
     *
     * @param {InputEvent} ev
     */
    onInput(ev) {
        this.state.query = ev.target.value;
        this.state.active = 0;
        this.state.open = true;
    }

    /** Clear the box and close the results. */
    clear() {
        this.state.query = "";
        this.state.open = false;
        this.input.el?.focus();
    }

    /** Close the results, keeping what was typed. */
    close() {
        this.state.open = false;
    }

    /**
     * Search the word the box offers instead.
     *
     * @param {string} word
     */
    useSuggestion(word) {
        this.state.query = word;
        this.state.active = 0;
        this.state.open = true;
        this.input.el?.focus();
    }

    /**
     * Open an item.
     *
     * @param {object} item a field or elsewhere item (see ``items``)
     */
    pick(item) {
        const query = this.state.query;
        this.state.open = false;
        this.state.query = "";
        this.input.el?.blur();
        if (item.kind === "field") {
            this.props.onPickField(item.field, query);
        } else {
            this.props.onPickElsewhere(item.entry);
        }
    }

    /**
     * ↑ ↓ move through the results, Enter opens one, Esc closes them.
     *
     * @param {KeyboardEvent} ev
     */
    onKeydown(ev) {
        const items = this.items;
        if (ev.key === "Escape") {
            if (this.state.open) {
                ev.preventDefault();
                ev.stopPropagation();
                this.close();
            }
            return;
        }
        if (!this.searching) {
            return;
        }
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
            ev.preventDefault();
            this.state.open = true;
            if (items.length) {
                const step = ev.key === "ArrowDown" ? 1 : -1;
                this.state.active = (this.state.active + step + items.length) % items.length;
            }
        } else if (ev.key === "Enter" && items[this.state.active]) {
            ev.preventDefault();
            this.pick(items[this.state.active]);
        }
    }

    /**
     * Close when the focus leaves the box and its results.
     *
     * @param {FocusEvent} ev
     */
    onFocusOut(ev) {
        if (!ev.currentTarget.contains(ev.relatedTarget)) {
            this.close();
        }
    }
}
