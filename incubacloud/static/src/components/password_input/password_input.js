import { Component, onWillUpdateProps, useEffect, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

let nextId = 0;

/**
 * PasswordInput — secure password field with show/hide and copy buttons.
 *
 * Stored passwords are never fetched back from the server. When a value is
 * already saved, the field shows «Stored» with a lock and a «Replace»
 * button, as Settings' secrets do (ESTANDAR-FORMULARIOS §8); Replace opens
 * an empty input with «Keep stored» beside it, which drops what was typed
 * and shows «Stored» again. With nothing stored, the input is empty and
 * says «Not set».
 *
 * Props:
 *   hasValue  — bool: whether a password is already stored server-side
 *   required  — bool (default false)
 *   emptyHint — str:  custom hint shown when no value is stored and the
 *                     user hasn't typed anything (takes the place of
 *                     autoGenerate's)
 *   autoGenerate — bool (default false): the server generates a strong
 *                     password when the field is left blank, which the hint
 *                     then says («Leave blank to auto-generate a strong
 *                     password»); without it and without emptyHint, no hint
 *   id        — str:  the input's id, for the form's ``<label for>``.
 *                     While «Stored» shows there is no input, so pressing
 *                     the label does nothing, as in Settings; Replace takes
 *                     ``<id>-replace``. The label carries ``<id>-label`` as
 *                     its own id: it describes Replace, which keeps the
 *                     name its text gives.
 *   ariaInvalid     — bool: the input is marked aria-invalid, as a plain
 *                     input of a row in error is
 *   ariaDescribedby — str: the id of the row's error message, which
 *                     describes the input before the hint does
 *   value     — str:  what the form holds for the field, which the input
 *                     shows: the field is controlled by its form, so a
 *                     field mounted again (a tab or a section shown again)
 *                     shows what the form will save. A value that is not
 *                     empty opens the field as Replace did. A value the
 *                     form changes by itself, not from the input (Discard
 *                     putting the page back), sets the field as it would
 *                     open on it: «Stored» again over a stored value when
 *                     the form holds none. Every form passes it; left out,
 *                     the field reads as empty.
 *   onChange  — (value: string) => void — called with the new value as the
 *               user types; called with "" when the field is cleared or
 *               the stored value kept (backend keeps the existing value
 *               when "" is received)
 */
export class PasswordInput extends Component {
    static template = "incubacloud.PasswordInput";
    static props = {
        hasValue:  { type: Boolean },
        required:  { type: Boolean, optional: true },
        emptyHint: { type: String, optional: true },
        autoGenerate: { type: Boolean, optional: true },
        id:        { type: String, optional: true },
        ariaInvalid:     { type: Boolean, optional: true },
        ariaDescribedby: { type: String, optional: true },
        value:     { type: String, optional: true },
        onChange:  { type: Function },
    };

    /**
     * Hold whether a stored value is being replaced (open already when the
     * form holds a new value), and move the focus when Replace or Keep
     * stored takes away the button it was on.
     */
    setup() {
        this.state = useState({
            visible:    false,  // show/hide typed value
            copied:     false,  // copy confirmation flash
            // Replace pressed over a stored value, or the form already
            // holds a new one: a field mounted again stays open on it.
            replacing:  !!this.value,
        });
        this.uid = `ic-password-${++nextId}`;
        this.inputRef = useRef("input");
        this.replaceRef = useRef("replace");
        // Where the focus goes once the control it was on is gone: "edit"
        // (the input opened) or "stored" (the Replace button), set by the
        // click and taken on the render that follows.
        this.focusNext = null;
        useEffect(() => this.restoreFocus(), () => [this.showsStored]);
        // The value this field last gave the form, to tell a change the
        // form made by itself from one typed here.
        this._lastSent = this.value;
        onWillUpdateProps((next) => this.followForm(next.value || ""));
    }

    /**
     * Follow a value the form changed by itself: the field shows it as it
     * would open on it, so a page put back as saved shows «Stored» (or an
     * empty input when nothing is stored) instead of an open, empty
     * Replace. A value this field sent is left as it is.
     *
     * @param {string} value what the form holds for the field now
     */
    followForm(value) {
        if (value !== this._lastSent) {
            this.state.replacing = !!value;
            this.state.visible = false;
        }
        this._lastSent = value;
    }

    /**
     * Give the focus to the control that took the place of the one the
     * reader pressed, so it does not fall to the page and a screen reader
     * keeps its place. Does nothing unless a click asked for it.
     */
    restoreFocus() {
        const target = this.focusNext;
        this.focusNext = null;
        if (target === "edit") {
            this.inputRef.el?.focus();
        } else if (target === "stored") {
            this.replaceRef.el?.focus();
        }
    }

    /** @returns {string} what the form holds for the field, "" when nothing */
    get value() {
        return this.props.value || "";
    }

    /** @returns {boolean} whether the stored value shows as «Stored», with Replace */
    get showsStored() {
        return this.props.hasValue && !this.state.replacing && !this.value;
    }

    /** @returns {string|undefined} the Replace button's id, apart from the input's */
    get replaceId() {
        return this.props.id ? `${this.props.id}-replace` : undefined;
    }

    /** @returns {string|undefined} the id of the hint line, which describes the input */
    get hintId() {
        return this.hint ? `${this.props.id || this.uid}-hint` : undefined;
    }

    /**
     * @returns {string|undefined} what describes the input: the row's
     *     error message, if any, then the hint line
     */
    get inputDescribedby() {
        const ids = [this.props.ariaDescribedby, this.hintId].filter(Boolean);
        return ids.length ? ids.join(" ") : undefined;
    }

    /** @returns {string|undefined} the label's id, which describes the Replace button */
    get labelId() {
        return this.props.id ? `${this.props.id}-label` : undefined;
    }

    /** @returns {string} the input's placeholder: «New value» over a stored one, else «Not set» */
    get placeholder() {
        return this.props.hasValue ? _t("New value") : _t("Not set");
    }

    /** @returns {string} the line under the input, or "" while nothing needs saying */
    get hint() {
        if (this.value) {
            return _t("New value entered — will be saved");
        }
        if (this.props.hasValue) {
            return "";
        }
        if (this.props.emptyHint) {
            return _t(this.props.emptyHint);
        }
        if (this.props.autoGenerate) {
            return _t("Leave blank to auto-generate a strong password");
        }
        return "";
    }

    // ── Stored value ────────────────────────────────────────────────────────

    /** Open the stored value for a new one, and move the focus to the input. */
    replace() {
        this.focusNext = "edit";
        this.state.replacing = true;
    }

    /** Keep the stored value: drop what was typed over it, and return the focus to Replace. */
    keep() {
        this.focusNext = "stored";
        this.state.replacing = false;
        this.state.visible = false;
        this._lastSent = "";
        this.props.onChange("");
    }

    // ── Typing ──────────────────────────────────────────────────────────────

    onInput(ev) {
        this._lastSent = ev.target.value;
        this.props.onChange(ev.target.value);
    }

    // ── Toggle visibility of typed value ────────────────────────────────────

    toggleVisible() {
        this.state.visible = !this.state.visible;
    }

    // ── Copy to clipboard ────────────────────────────────────────────────────

    async copyToClipboard() {
        const value = this.value;
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
