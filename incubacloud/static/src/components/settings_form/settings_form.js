/** @odoo-module **/

/**
 * The pieces a Settings page is built from (ESTANDAR-FORMULARIOS §2–§8).
 *
 * SettingsBlock   a card with a title, and a long explanation folded in
 *                 "How it works".
 * SettingRow      one setting: label and help on the left, the control on
 *                 the right, its error under the control. Marks a changed
 *                 value with a dot, and the field the search led to.
 * SettingField    a SettingRow with the control its declaration asks for.
 * SettingAdvanced the fields most installations never touch, folded at the
 *                 end of a block; opens by itself for the search or an error.
 *
 * Labels, help and rules come from the field's declaration
 * (settings_registry.js). What changed, what is wrong and which field the
 * search found come from the Settings page (env.settingsForm), which owns
 * the save bar.
 */

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { RlSelect } from "../rl_select/rl_select";
import { fieldProblem, settingField } from "../settings/settings_registry";

let nextBlockId = 0;

/**
 * The page's shared state for the rows, or an inert one outside Settings.
 *
 * @param {object} env the component environment
 * @returns {object} env.settingsForm, or a stand-in that tracks nothing
 */
function formApi(env) {
    return env.settingsForm || {
        store: { saved: {}, errors: {}, found: null },
        isChanged: () => false,
        setError: () => {},
    };
}

/** A card of settings that belong together. */
export class SettingsBlock extends Component {
    static template = "incubacloud.SettingsBlock";
    static props = {
        title: { type: String, optional: true },
        how: { type: String, optional: true },
        slots: { type: Object, optional: true },
    };

    /** Give the title an id for the card's aria-labelledby. */
    setup() {
        this.titleId = `ic-set-block-${++nextBlockId}`;
        this.state = useState({ how: false });
    }
}

/** One setting: its words on the left, its control on the right. */
export class SettingRow extends Component {
    static template = "incubacloud.SettingRow";
    static props = {
        id: String,
        label: { type: String, optional: true },
        help: { type: String, optional: true },
        // The control under the words at every width (long lists, PEM).
        stacked: { type: Boolean, optional: true },
        warning: { type: String, optional: true },
        // The form holding the value, when the row draws its own control:
        // read here so the row's dot follows what is typed.
        form: { type: Object, optional: true },
        slots: { type: Object, optional: true },
    };

    /** Subscribe to the page's state, so the dot and the error follow it. */
    setup() {
        this.api = formApi(this.env);
        this.store = useState(this.api.store);
        this.form = this.props.form ? useState(this.props.form) : null;
    }

    /** @returns {object|null} the field's declaration */
    get field() {
        return settingField(this.props.id);
    }

    /** @returns {string} the label shown: the prop's, else the declaration's */
    get label() {
        return String(this.props.label ?? this.field?.label ?? "");
    }

    /** @returns {string} the help line shown, or "" */
    get help() {
        return String(this.props.help ?? this.field?.help ?? "");
    }

    /** @returns {string} the id of the row, the target of #<field> */
    get rowId() {
        return `ic-set-${this.props.id}`;
    }

    /** @returns {string} the id the control takes, for the label's ``for`` */
    get inputId() {
        return `ic-set-${this.props.id}-input`;
    }

    /** @returns {string|null} the error under the control */
    get error() {
        return this.store.errors[this.props.id] || null;
    }

    /** @returns {boolean} whether the value differs from what is saved */
    get changed() {
        void this.form?.[this.props.id];
        return this.api.isChanged(this.props.id, this.store);
    }

    /** @returns {boolean} whether the search led here */
    get found() {
        return this.store.found === this.props.id;
    }

    /** @returns {string} the ids of the help and the error, for the control */
    get describedBy() {
        return [
            this.help && `${this.rowId}-help`,
            this.error && `${this.rowId}-error`,
            this.props.warning && `${this.rowId}-warning`,
        ].filter(Boolean).join(" ");
    }
}

/** A setting with the control its declaration asks for. */
export class SettingField extends Component {
    static template = "incubacloud.SettingField";
    static components = { SettingRow, RlSelect };
    static props = {
        id: String,
        form: Object,
        // Options computed at run time (hosts, destinations); else the
        // declaration's.
        options: { type: Array, optional: true },
        emptyLabel: { type: String, optional: true },
        // A secret: whether one is saved on the server.
        stored: { type: Boolean, optional: true },
        disabled: { type: Boolean, optional: true },
        readonly: { type: Boolean, optional: true },
        placeholder: { type: String, optional: true },
        // Overrides of the declaration, when the words depend on state.
        label: { type: String, optional: true },
        help: { type: String, optional: true },
        warning: { type: String, optional: true },
        onChange: { type: Function, optional: true },
        slots: { type: Object, optional: true },
    };

    /** Read the form through this component, so it re-renders with it. */
    setup() {
        this.api = formApi(this.env);
        this.form = useState(this.props.form);
        this.state = useState({ replacing: false });
    }

    /** @returns {object} the field's declaration */
    get field() {
        return settingField(this.props.id) || { id: this.props.id, type: "text" };
    }

    /** @returns {string} the label, as text, for a control named by it */
    get labelText() {
        return String(this.props.label ?? this.field.label ?? "");
    }

    /** @returns {*} the value the form holds */
    get value() {
        return this.form[this.props.id];
    }

    /** @returns {string} the CSS width class: s, m or l */
    get width() {
        const field = this.field;
        if (field.width) {
            return field.width;
        }
        return { number: "s", textarea: "l", secret: "m", select: "m" }[field.type] || "m";
    }

    /** @returns {object[]} the select's options */
    get options() {
        return this.props.options || this.field.options || [];
    }

    /** @returns {boolean} whether a stored secret shows as stored */
    get showsStored() {
        return Boolean(this.props.stored) && !this.state.replacing && !this.value;
    }

    /**
     * Put a typed value in the form.
     *
     * A number is kept as a number when it reads as one and as typed
     * otherwise: nothing typed is ever erased, and the check on leaving
     * the field says what is wrong with it.
     *
     * @param {*} raw the control's value
     */
    set(raw) {
        let value = raw;
        if (this.field.type === "number" && typeof raw === "string") {
            const text = raw.trim();
            value = text !== "" && Number.isFinite(Number(text)) ? Number(text) : raw;
        }
        this.form[this.props.id] = value;
        if (this.api.store.errors[this.props.id]) {
            this.check();
        }
        this.props.onChange?.(value);
    }

    /** Check the value when the field is left, and show what is wrong. */
    check() {
        this.api.setError(
            this.props.id, fieldProblem(this.field, this.value, this.form),
        );
    }

    /** Flip a switch. */
    toggle() {
        if (!this.props.disabled && !this.props.readonly) {
            this.set(!this.value);
        }
    }

    /** @returns {string} what an empty secret field says */
    get notSetText() {
        return _t("Not set");
    }

    /** @returns {string} what a secret field opened over a stored one says */
    get newValueText() {
        return _t("New value");
    }

    /** Open a stored secret for a new value. */
    replace() {
        this.state.replacing = true;
    }

    /** Keep the stored secret: drop what was typed over it. */
    keep() {
        this.state.replacing = false;
        this.set("");
    }
}

/** The fields of a block that most installations never touch. */
export class SettingAdvanced extends Component {
    static template = "incubacloud.SettingAdvanced";
    static props = {
        // The ids of the fields inside, to count them and their changes.
        fields: { type: Array, element: String },
        // The form holding them, read so the count of changes follows it.
        form: { type: Object, optional: true },
        slots: { type: Object, optional: true },
    };

    /** Folded, unless the search or an error is inside (see ``open``). */
    setup() {
        this.api = formApi(this.env);
        this.store = useState(this.api.store);
        this.form = this.props.form ? useState(this.props.form) : null;
        this.state = useState({ open: false });
        this.panelId = `ic-set-adv-${++nextBlockId}`;
    }

    /** @returns {boolean} open by hand, or for a field found or in error */
    get open() {
        return this.state.open || this.props.fields.some(
            (id) => this.store.found === id || this.store.errors[id],
        );
    }

    /** @returns {number} how many of its fields differ from what is saved */
    get changedCount() {
        for (const id of this.props.fields) {
            void this.form?.[id];
        }
        return this.props.fields.filter((id) => this.api.isChanged(id, this.store)).length;
    }

    /** @returns {string} "N settings" */
    get countLabel() {
        const n = this.props.fields.length;
        return n === 1 ? _t("1 setting") : _t("%s settings", n);
    }

    /** Fold or unfold by hand. */
    toggle() {
        this.state.open = !this.open;
        if (!this.state.open && this.store.found && this.props.fields.includes(this.store.found)) {
            this.store.found = null;
        }
    }
}
