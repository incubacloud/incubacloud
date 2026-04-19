import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * PasswordInput — secure password field with show/hide and copy buttons.
 *
 * Stored passwords are never fetched back from the server. When a value is
 * already saved, the field shows a visual "set" indicator and a hint telling
 * the user to type a new value only if they want to replace the current one.
 *
 * Props:
 *   hasValue  — bool: whether a password is already stored server-side
 *   required  — bool (default false)
 *   onChange  — (value: string) => void — called with the new value as the
 *               user types; called with "" when the field is cleared
 *               (backend keeps the existing value when "" is received)
 */
export class PasswordInput extends Component {
    static template = "incubacloud.PasswordInput";
    static props = {
        hasValue:  { type: Boolean },
        required:  { type: Boolean, optional: true },
        onChange:  { type: Function },
    };

    setup() {
        this.state = useState({
            visible:    false,  // show/hide typed value
            copied:     false,  // copy confirmation flash
            inputValue: "",     // current value in the input box
        });
    }

    /** True when a value is stored server-side and the user has not typed a replacement. */
    get isSet() {
        return this.props.hasValue && !this.state.inputValue;
    }

    get hint() {
        if (this.state.inputValue) {
            return _t("New value entered — will be saved");
        }
        if (this.props.hasValue) {
            return _t("Value stored — type a new one to replace it");
        }
        return _t("Leave blank to auto-generate a strong password");
    }

    // ── Typing ──────────────────────────────────────────────────────────────

    onInput(ev) {
        this.state.inputValue = ev.target.value;
        this.props.onChange(ev.target.value);
    }

    // ── Toggle visibility of typed value ────────────────────────────────────

    toggleVisible() {
        this.state.visible = !this.state.visible;
    }

    // ── Copy to clipboard ────────────────────────────────────────────────────

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
