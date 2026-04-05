import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

/**
 * PasswordInput — secure password field with reveal and copy buttons.
 *
 * Props:
 *   hasValue   — bool: whether a password is already stored server-side
 *   model      — string: Odoo model name (e.g. 'cloud.instance')
 *   recordId   — int|null: record id (null when creating a new record)
 *   fieldName  — string: field name on the model
 *   required   — bool (default false)
 *   onChange   — (value: string) => void  — called with new value as user types
 *                (called with "" when user clears; backend keeps existing if "")
 */
export class PasswordInput extends Component {
    static template = "incubacloud.PasswordInput";
    static props = {
        hasValue:  { type: Boolean },
        model:     { type: String },
        recordId:  { type: [Number, { value: null }], optional: true },
        fieldName: { type: String },
        required:  { type: Boolean, optional: true },
        onChange:  { type: Function },
    };

    setup() {
        this.state = useState({
            visible:   false,   // show/hide typed value
            revealing: false,   // loading reveal from server
            revealed:  false,   // server value is currently shown
            copied:    false,   // copy confirmation flash
            inputValue: "",     // current value in the input box
        });
    }

    get hint() {
        if (this.props.hasValue) {
            return "Password set — leave blank to keep current";
        }
        return "Leave blank to auto-generate a strong password";
    }

    // ── Typing ──────────────────────────────────────────────────────────────

    onInput(ev) {
        this.state.inputValue = ev.target.value;
        this.state.revealed = false;
        this.props.onChange(ev.target.value);
    }

    // ── Toggle visibility of typed value ────────────────────────────────────

    toggleVisible() {
        this.state.visible = !this.state.visible;
    }

    // ── Reveal current server-side password ─────────────────────────────────

    async reveal() {
        if (!this.props.recordId || this.state.revealing) return;
        this.state.revealing = true;
        try {
            const res = await rpc("/cloud/get_secret", {
                model: this.props.model,
                record_id: this.props.recordId,
                field: this.props.fieldName,
            });
            if (res && res.value) {
                this.state.inputValue = res.value;
                this.state.visible = true;
                this.state.revealed = true;
                // Notify parent so it can track the value if needed,
                // but pass a sentinel so the backend knows not to save it
                // (parent should send "" when revealed === true and user didn't type)
            }
        } catch {
            // silent — leave field empty
        } finally {
            this.state.revealing = false;
        }
    }

    // ── Copy to clipboard ───────────────────────────────────────────────────

    async copyToClipboard() {
        const value = this.state.inputValue;
        if (!value) return;
        try {
            await navigator.clipboard.writeText(value);
            this.state.copied = true;
            setTimeout(() => { this.state.copied = false; }, 2000);
        } catch {
            // Clipboard API not available
        }
    }
}
