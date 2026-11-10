/** @odoo-module **/

import { Component, onPatched, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

import {
    placeAnchoredMenu,
    useAnchoredMenu,
    viewportHeight,
} from "../../utils/use_anchored_menu";

// Menu geometry, in px. The cap equals ``.ic-multi-select__menu``'s
// ``max-block-size`` in multi_select.scss; a row is an option's 8 px padding
// twice plus its text line (19.5 px at 13 px); the chrome is the menu's 5 px
// padding twice plus its 1 px border twice.
const MENU_MAX_HEIGHT = 280;
const MENU_ROW_HEIGHT = 35.5;
const MENU_CHROME_HEIGHT = 12;

/**
 * MultiSelect — generic searchable multi-value select.
 *
 * The chosen options show as chips, each with its own remove button; a
 * search field beside them filters the others in a menu, which stays open
 * between picks. The menu is ``position:fixed`` (like SearchSelect's), so it
 * escapes any ``overflow:hidden`` ancestor, and its list scrolls: a long
 * listing (hundreds of models, many users) keeps the field small.
 *
 * Keyboard: arrows move through the menu, Enter picks, Escape closes, and
 * Backspace in the empty field removes the last chip.
 *
 * Props:
 *   options     {{value, label, hint?, disabled?}[]}  the options; ``hint``
 *               is shown muted beside the label in the menu, and a
 *               ``disabled`` one cannot be picked (a chosen one can still
 *               be removed)
 *   value       {Array}    the values chosen, in the order picked
 *   onChange    {(values: Array) => void}  called with the new list
 *   id          {string}   the search field's id, for a ``<label for>``
 *   placeholder {string}   the search field's text while nothing is typed
 *   emptyText   {string}   the menu's text when no option is left
 *   disabled    {boolean}  nothing can be picked or removed
 *   ariaLabel   {string}   the search field's accessible name
 */
export class MultiSelect extends Component {
    static template = "incubacloud.MultiSelect";
    static props = {
        options: { type: Array },
        value: { type: Array },
        onChange: { type: Function },
        id: { type: String, optional: true },
        placeholder: { type: String, optional: true },
        emptyText: { type: String, optional: true },
        disabled: { type: Boolean, optional: true },
        ariaLabel: { type: String, optional: true },
    };
    static defaultProps = {
        placeholder: _t("Search…"),
        emptyText: _t("No results"),
        disabled: false,
    };

    /** Keep the search, whether the menu is open, and its active option. */
    setup() {
        this.state = useState({ input: "", open: false, active: 0, menuStyle: "" });
        this.wrapRef = useRef("wrap");
        this.inputRef = useRef("input");
        // The menu is fixed: it follows the field on scroll and resize.
        useAnchoredMenu(() => this._placeMenu());
        // The parent hands the new value down after a pick, and the options
        // or the chips may change under an open menu: place it again once
        // the rows it shows are the ones left.
        onPatched(() => this._placeMenu());
    }

    /**
     * The chosen options, in the order of ``value``. A value no option has
     * (a listing read again since) still shows, by its value, to be removed.
     *
     * @returns {{value, label, disabled: boolean}[]}
     */
    get chips() {
        const byValue = new Map(this.props.options.map((option) => [option.value, option]));
        return this.props.value.map((value) => {
            const option = byValue.get(value);
            return {
                value,
                label: option ? option.label : String(value),
                disabled: Boolean(option?.disabled),
            };
        });
    }

    /**
     * The options not chosen yet whose label or hint holds the search.
     *
     * @returns {{value, label, hint?, disabled?}[]}
     */
    get filtered() {
        const query = this.state.input.trim().toLowerCase();
        const chosen = new Set(this.props.value);
        return this.props.options.filter(
            (option) =>
                !chosen.has(option.value) &&
                (!query ||
                    String(option.label).toLowerCase().includes(query) ||
                    String(option.hint || "").toLowerCase().includes(query))
        );
    }

    /** Height of the viewport the fixed menu is placed in (a seam for the tests). */
    _viewportHeight() {
        return viewportHeight();
    }

    /**
     * Height the open menu would have, capped like the stylesheet's
     * ``max-block-size``: one row per option left, or the empty text's.
     *
     * @returns {number} pixels
     */
    _menuHeight() {
        const rows = Math.max(this.filtered.length, 1);
        return Math.min(MENU_MAX_HEIGHT, rows * MENU_ROW_HEIGHT + MENU_CHROME_HEIGHT);
    }

    /**
     * Fixed-position style beside the field, as wide as it: below it, or
     * above it when it does not fit below and there is more room there,
     * with its height limited to that side's room (``placeAnchoredMenu``).
     *
     * @returns {string} the inline style, "" while the field is not mounted
     */
    _menuStyle() {
        const el = this.wrapRef.el;
        if (!el) {
            return "";
        }
        const rect = el.getBoundingClientRect();
        const { side, maxHeight } = placeAnchoredMenu(rect, this._viewportHeight(), {
            height: this._menuHeight(),
            maxHeight: MENU_MAX_HEIGHT,
        });
        return `position:fixed;${side};left:${rect.left}px;width:${rect.width}px;max-block-size:${maxHeight}px`;
    }

    /**
     * Place the open menu again, for the rows it lists now: the side it fits
     * on and its height change with them. Nothing is written when the style
     * is the same, or while the menu is closed.
     */
    _placeMenu() {
        if (!this.state.open) {
            return;
        }
        const style = this._menuStyle();
        if (style !== this.state.menuStyle) {
            this.state.menuStyle = style;
        }
    }

    /** Open the menu beside the field, its first option active. */
    open() {
        if (this.props.disabled || this.state.open) {
            return;
        }
        this.state.menuStyle = this._menuStyle();
        this.state.active = 0;
        this.state.open = true;
    }

    /** A click anywhere on the field puts the cursor in the search, menu open. */
    onFieldClick() {
        this.inputRef.el?.focus();
        this.open();
    }

    /** Close the menu and forget the search. */
    close() {
        this.state.open = false;
        this.state.input = "";
    }

    /** Close once the field loses focus, after a pick's mousedown has run. */
    onBlur() {
        setTimeout(() => this.close(), 160);
    }

    /** Filter on what is typed; the menu opens on the first match. */
    onInput(ev) {
        this.state.input = ev.target.value;
        this.state.active = 0;
        this.open();
        this._placeMenu();
    }

    /**
     * Choose *option*: it joins the end of the list. A disabled one is not
     * chosen. The menu stays open for the next pick.
     *
     * @param {{value, disabled?}} option
     */
    pick(option) {
        if (this.props.disabled || option.disabled || this.props.value.includes(option.value)) {
            return;
        }
        this.props.onChange([...this.props.value, option.value]);
        this.state.input = "";
        this.state.active = 0;
        this._placeMenu();
    }

    /**
     * Take *value* out of the list.
     *
     * @param {*} value
     */
    remove(value) {
        if (this.props.disabled) {
            return;
        }
        this.props.onChange(this.props.value.filter((chosen) => chosen !== value));
    }

    /**
     * Arrows move through the menu, Enter picks the active option, Escape
     * closes, and Backspace in an empty field removes the last chip.
     *
     * @param {KeyboardEvent} ev
     */
    onKeydown(ev) {
        const options = this.filtered;
        switch (ev.key) {
            case "ArrowDown":
            case "ArrowUp": {
                ev.preventDefault();
                if (!this.state.open) {
                    this.open();
                    return;
                }
                const step = ev.key === "ArrowDown" ? 1 : -1;
                const count = options.length || 1;
                this.state.active = (this.state.active + step + count) % count;
                return;
            }
            case "Enter":
                if (this.state.open && options[this.state.active]) {
                    ev.preventDefault();
                    this.pick(options[this.state.active]);
                }
                return;
            case "Escape":
                if (this.state.open) {
                    ev.preventDefault();
                    this.close();
                }
                return;
            case "Backspace":
                if (!this.state.input && this.props.value.length) {
                    this.remove(this.props.value[this.props.value.length - 1]);
                }
                return;
        }
    }

    /** The id of the menu, for the field's ``aria-controls``. */
    get menuId() {
        return `${this.props.id || "ic-multi-select"}-menu`;
    }

    /**
     * The accessible name of a chip's remove button.
     *
     * @param {{label: string}} chip
     * @returns {string}
     */
    removeLabel(chip) {
        return _t("Remove %(label)s", { label: chip.label });
    }
}
