/** @odoo-module **/

import { Component, useState, useRef, onWillUnmount } from "@odoo/owl";

import { useAnchoredMenu } from "../../utils/use_anchored_menu";

// Menu geometry, in px. The cap equals ``.rl-sel__menu``'s ``max-block-size``
// in relay.scss; a row is an option's 8 px padding twice plus its text line
// (19.5 px, measured at 13 px); the chrome is the menu's 5 px padding twice
// plus its 1 px border twice.
const MENU_MAX_HEIGHT = 280;
const MENU_MIN_HEIGHT = 96;
const MENU_ROW_HEIGHT = 35.5;
const MENU_CHROME_HEIGHT = 12;
// Gap between trigger and menu, and margin kept to the window's edge.
const MENU_GAP = 4;
const MENU_EDGE = 8;

/**
 * RlSelect — custom single-select dropdown styled with the Relay design tokens.
 *
 * Replaces the native <select> because its option popup, on Linux/GTK Chromium,
 * is drawn by the OS and ignores CSS (accent-color, option background): the list
 * renders with the system blue highlight regardless of our theme. RlSelect draws
 * its own list with rl-* classes so it always honours the carbon/green palette.
 *
 * It behaves like a single-value <select>: the trigger shows the selected
 * option's label, clicking opens a fixed-position menu (so it escapes any
 * overflow:hidden / scroll ancestor, exactly like SearchSelect), and picking an
 * option calls onChange(value) — the raw value, never a DOM event.
 *
 * Props:
 *   options     {string[]|{value,label,disabled?}[]}  selectable options
 *   value       {string|number|null}                  currently selected value
 *   onChange    {(value) => void}                      called with the picked value
 *   emptyLabel  {string}   optional "clear" row (value ""); also the trigger text
 *                          shown while the value is empty
 *   placeholder {string}   trigger text when nothing is selected and no emptyLabel
 *   disabled    {boolean}  disable interaction
 *   ariaLabel   {string}   accessible label for the trigger button
 */
export class RlSelect extends Component {
    static template = "incubacloud.RlSelect";
    static props = {
        options:     { type: Array },
        value:       { optional: true },
        onChange:    { type: Function },
        emptyLabel:  { type: String, optional: true },
        placeholder: { type: String, optional: true },
        disabled:    { type: Boolean, optional: true },
        ariaLabel:   { type: String, optional: true },
        // Mirror what a plain <input> exposes when its row is in error,
        // so a screen reader announces the failure on the control the
        // user is standing on — not only in the caption below it.
        ariaInvalid:      { type: Boolean, optional: true },
        ariaDescribedby:  { type: String, optional: true },
    };
    static defaultProps = {
        disabled: false,
    };

    setup() {
        this.state = useState({ open: false, dropdownStyle: "" });
        this.wrapRef = useRef("wrap");
        this._onDocMouseDown = this._onDocMouseDown.bind(this);
        onWillUnmount(() =>
            document.removeEventListener("mousedown", this._onDocMouseDown, true)
        );
        // The menu is position:fixed, so it must follow the trigger when
        // anything scrolls or the window resizes.
        useAnchoredMenu(() => {
            if (this.state.open) {
                this.state.dropdownStyle = this._computeDropdownStyle();
            }
        });
    }

    // ── Option normalisation ────────────────────────────────────────────────

    /** Normalise options to [{value, label, disabled}] regardless of input shape. */
    get _opts() {
        return this.props.options.map((o) =>
            typeof o === "object" && o !== null
                ? { value: o.value, label: o.label, disabled: !!o.disabled }
                : { value: o, label: String(o), disabled: false }
        );
    }

    /** True when the current value is empty (null/undefined/""). */
    get isPlaceholder() {
        const v = this.props.value;
        return v === null || v === undefined || v === "";
    }

    /**
     * Loose value equality. Native <select> values are always strings, but
     * callers often store numeric ids, so compare stringified to avoid
     * mis-highlighting (e.g. value 5 vs option value "5").
     */
    _eq(a, b) {
        return String(a) === String(b);
    }

    /** Whether the given option is the current selection. */
    isSelected(opt) {
        return !this.isPlaceholder && this._eq(opt.value, this.props.value);
    }

    /** Label shown in the trigger for the current value. */
    get displayLabel() {
        if (this.isPlaceholder) {
            return this.props.emptyLabel || this.props.placeholder || "";
        }
        const match = this._opts.find((o) => this._eq(o.value, this.props.value));
        return match ? match.label : String(this.props.value);
    }

    // ── Dropdown positioning ─────────────────────────────────────────────────

    /** Height of the viewport the fixed menu is placed in (a seam for the tests). */
    _viewportHeight() {
        return document.documentElement.clientHeight;
    }

    /**
     * Height the open menu would have, capped like the stylesheet's
     * ``max-block-size``: its rows, one per option plus the clear row, with
     * the menu's padding and border.
     *
     * @returns {number} pixels
     */
    _menuHeight() {
        const rows = this._opts.length + (this.props.emptyLabel ? 1 : 0);
        return Math.min(MENU_MAX_HEIGHT, rows * MENU_ROW_HEIGHT + MENU_CHROME_HEIGHT);
    }

    /**
     * Fixed-position style anchored to the trigger, escaping overflow clipping.
     *
     * The menu opens below the trigger, as a native select does. When its
     * height does not fit there and the space above is larger it opens
     * upwards instead, anchored by ``bottom`` so it grows away from the
     * trigger whatever its real height. Its ``max-block-size`` is the space
     * of the chosen side (at least ``MENU_MIN_HEIGHT``, at most the
     * stylesheet's cap), so a long list scrolls inside the window rather
     * than leaving it.
     *
     * @returns {string} the inline style, "" while the trigger is not mounted
     */
    _computeDropdownStyle() {
        const el = this.wrapRef.el;
        if (!el) {
            return "";
        }
        const r = el.getBoundingClientRect();
        const viewport = this._viewportHeight();
        const below = viewport - r.bottom - MENU_GAP - MENU_EDGE;
        const above = r.top - MENU_GAP - MENU_EDGE;
        const upwards = below < this._menuHeight() && above > below;
        const maxHeight = Math.round(
            Math.max(MENU_MIN_HEIGHT, Math.min(MENU_MAX_HEIGHT, upwards ? above : below))
        );
        const side = upwards
            ? `bottom:${Math.round(viewport - r.top + MENU_GAP)}px`
            : `top:${Math.round(r.bottom + MENU_GAP)}px`;
        return `position:fixed;${side};left:${r.left}px;min-width:${r.width}px;max-block-size:${maxHeight}px`;
    }

    // ── Open / close ──────────────────────────────────────────────────────────

    /** Toggle the menu open/closed (no-op when disabled). */
    toggle() {
        if (this.props.disabled) {
            return;
        }
        if (this.state.open) {
            this._close();
        } else {
            this.state.dropdownStyle = this._computeDropdownStyle();
            this.state.open = true;
            document.addEventListener("mousedown", this._onDocMouseDown, true);
        }
    }

    /** Close the menu and detach the outside-click listener. */
    _close() {
        this.state.open = false;
        document.removeEventListener("mousedown", this._onDocMouseDown, true);
    }

    /** Close when a mousedown lands outside the trigger and its menu. */
    _onDocMouseDown(ev) {
        const wrap = this.wrapRef.el;
        if (wrap && wrap.contains(ev.target)) {
            return;
        }
        if (ev.target && ev.target.closest && ev.target.closest(".rl-sel__menu")) {
            return;
        }
        this._close();
    }

    // ── Selection ──────────────────────────────────────────────────────────────

    /** Pick an option: notify the parent with its raw value and close. */
    pick(opt) {
        if (opt.disabled) {
            return;
        }
        this.props.onChange(opt.value);
        this._close();
    }

    /** Pick the empty/clear row (value ""). */
    pickEmpty() {
        this.props.onChange("");
        this._close();
    }

    /** Close the menu on Escape for keyboard users. */
    onKeydown(ev) {
        if (ev.key === "Escape" && this.state.open) {
            this._close();
        }
    }
}
