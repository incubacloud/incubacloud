import { Component, useState } from "@odoo/owl";

/**
 * ModuleSelector — chip-based addon module selector.
 *
 * Props:
 *   value        — comma-separated string of currently selected module names
 *   onChange     — (newValue: string) => void
 *   modules      — string[] — full list of available module names (from GitHub)
 *   loading      — bool — show spinner while modules are being fetched
 *   error        — string|null — error message to display
 *   disabled     — bool — disable the input (no branch selected yet)
 *   blockedSet   — Set<string> — names already in the sibling selector
 *                  (used for overlap validation / exclusion from dropdown)
 *   placeholder  — string
 *   emptyLabel   — string — label shown when value is empty (e.g. "ALL", "NONE")
 *   showEmpty    — bool — whether to show the emptyLabel chip when value is empty
 */
export class ModuleSelector extends Component {
    static template = "incubacloud.ModuleSelector";
    static props = {
        value:       { type: String },
        onChange:    { type: Function },
        modules:     { type: Array },
        loading:     { type: Boolean, optional: true },
        error:       { type: [String, Boolean], optional: true },
        disabled:    { type: Boolean, optional: true },
        blockedSet:  { type: Object, optional: true },   // Set
        placeholder: { type: String, optional: true },
        emptyLabel:  { type: String, optional: true },
        showEmpty:   { type: Boolean, optional: true },
        resetLabel:  { type: String, optional: true },   // label for the "clear selection" button
        onOpen:      { type: Function, optional: true }, // called on first focus
    };
    static defaultProps = {
        loading:     false,
        error:       null,
        disabled:    false,
        blockedSet:  null,
        placeholder: "Add module…",
        emptyLabel:  "ALL",
        showEmpty:   true,
        resetLabel:  null,
        onOpen:      null,
    };

    setup() {
        this.state = useState({ input: "", open: false });
    }

    // ── Derived ────────────────────────────────────────────────────────────

    get selected() {
        return (this.props.value || "")
            .split(",").map(s => s.trim()).filter(Boolean);
    }

    get isEmpty() {
        return this.selected.length === 0;
    }

    get filtered() {
        const q   = this.state.input.toLowerCase();
        const sel = new Set(this.selected);
        const blk = this.props.blockedSet || new Set();
        return this.props.modules.filter(
            m => !sel.has(m) && !blk.has(m) && (!q || m.toLowerCase().includes(q))
        );
    }

    get showCreate() {
        const q = this.state.input.trim();
        const blk = this.props.blockedSet || new Set();
        return (
            q.length > 0 &&
            !this.props.modules.includes(q) &&
            !this.selected.includes(q) &&
            !blk.has(q)
        );
    }

    get overlapError() {
        const blk = this.props.blockedSet;
        if (!blk || !blk.size) return null;
        const overlap = this.selected.filter(m => blk.has(m));
        if (!overlap.length) return null;
        return `Conflict: "${overlap.join('", "')}" appears in both Include and Exclude`;
    }

    // ── Handlers ───────────────────────────────────────────────────────────

    add(name) {
        const name_ = (name || this.state.input).trim();
        if (!name_) return;
        const next = new Set(this.selected);
        next.add(name_);
        this.props.onChange([...next].join(","));
        this.state.input = "";
        this.state.open  = false;
    }

    remove(name) {
        const next = new Set(this.selected);
        next.delete(name);
        this.props.onChange([...next].join(","));
    }

    resetToEmpty() {
        this.props.onChange("");
    }

    onInput(ev) {
        this.state.input = ev.target.value;
        this.state.open  = true;
    }

    onFocus() {
        this.state.open = true;
        this.props.onOpen?.();
    }

    onBlur() {
        setTimeout(() => { this.state.open = false; }, 160);
    }
}
