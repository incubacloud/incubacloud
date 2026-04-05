import { Component, useState, useRef } from "@odoo/owl";

/**
 * SearchSelect — generic searchable single-select widget.
 *
 * Accepts options as plain strings or {value, label} objects.
 * Supports free-text entry (allowFreeText) for cases like branch names
 * where the user may type a value not in the fetched list.
 *
 * Props:
 *   options      {string[]|{value,label}[]}  available options
 *   value        {string|null}               currently selected value
 *   onChange     {(value: string) => void}   called on every change
 *   placeholder  {string}                    input placeholder text
 *   loading      {boolean}                   show spinner while loading
 *   disabled     {boolean}                   disable the input
 *   allowFreeText {boolean}                  accept values not in options
 *   emptyLabel   {string}                    label shown for null/empty value
 *   onOpen       {() => void}                called when dropdown opens (lazy-load hook)
 */
export class SearchSelect extends Component {
    static template = "incubacloud.SearchSelect";
    static props = {
        options:       { type: Array },
        value:         { optional: true },
        onChange:      { type: Function },
        placeholder:   { type: String, optional: true },
        loading:       { type: Boolean, optional: true },
        disabled:      { type: Boolean, optional: true },
        allowFreeText: { type: Boolean, optional: true },
        emptyLabel:    { type: String, optional: true },
        onOpen:        { type: Function, optional: true },
    };
    static defaultProps = {
        placeholder:   "Search…",
        loading:       false,
        disabled:      false,
        allowFreeText: false,
        emptyLabel:    "— select —",
    };

    setup() {
        this.state = useState({ input: "", open: false, dropdownStyle: "" });
        this.wrapRef = useRef("wrap");
    }

    // ── Option normalisation ───────────────────────────────────────────────

    /** Normalise options to [{value, label}] regardless of input format. */
    get _opts() {
        return this.props.options.map(o =>
            typeof o === "object" && o !== null
                ? o
                : { value: o, label: String(o) }
        );
    }

    /** Label for the currently selected value. */
    get _selectedLabel() {
        const v = this.props.value;
        if (v === null || v === undefined || v === "") return "";
        const match = this._opts.find(o => o.value === v);
        return match ? match.label : String(v);
    }

    // ── Filtered list ─────────────────────────────────────────────────────

    get filtered() {
        const q = this.state.input.toLowerCase();
        if (!q) return this._opts;
        return this._opts.filter(o => o.label.toLowerCase().includes(q));
    }

    get showFreeTextCreate() {
        if (!this.props.allowFreeText) return false;
        const q = this.state.input.trim();
        if (!q) return false;
        return !this._opts.some(o => o.label.toLowerCase() === q.toLowerCase());
    }

    // ── Dropdown positioning ───────────────────────────────────────────────

    _computeDropdownStyle() {
        const el = this.wrapRef.el;
        if (!el) return "";
        const r = el.getBoundingClientRect();
        return `position:fixed;top:${r.bottom}px;left:${r.left}px;width:${r.width}px`;
    }

    // ── Events ────────────────────────────────────────────────────────────

    onFocus() {
        this.state.input = "";
        this.state.dropdownStyle = this._computeDropdownStyle();
        this.state.open = true;
        this.props.onOpen?.();
    }

    onInput(ev) {
        this.state.input = ev.target.value;
        this.state.open = true;
        if (this.props.allowFreeText) {
            this.props.onChange(ev.target.value);
        }
    }

    onBlur() {
        // Delay so mousedown on an option fires before the dropdown closes.
        setTimeout(() => {
            this.state.open = false;
            // If no free text allowed, restore the committed label on blur
            // so the input never shows a partial/stale search string.
            if (!this.props.allowFreeText) {
                this.state.input = "";
            }
        }, 160);
    }

    pick(opt) {
        this.props.onChange(opt.value);
        this.state.input = "";
        this.state.open = false;
    }

    pickFreeText() {
        const q = this.state.input.trim();
        if (!q) return;
        this.props.onChange(q);
        this.state.input = "";
        this.state.open = false;
    }

    // ── Display value shown in the input ─────────────────────────────────

    get displayValue() {
        // While the dropdown is open, show what the user is typing.
        // When closed, show the label of the selected option.
        return this.state.open ? this.state.input : (this._selectedLabel || "");
    }
}
