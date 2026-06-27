/** @odoo-module **/

import { Component, useState, useRef, onWillUnmount } from "@odoo/owl";

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

    /** Fixed-position style anchored to the trigger, escaping overflow clipping. */
    _computeDropdownStyle() {
        const el = this.wrapRef.el;
        if (!el) {
            return "";
        }
        const r = el.getBoundingClientRect();
        return `position:fixed;top:${r.bottom + 4}px;left:${r.left}px;min-width:${r.width}px`;
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
