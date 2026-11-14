import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, edit, queryOne, waitFor } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, useState, xml } from "@odoo/owl";
import { allowTranslations, mountWithCleanup } from "@web/../tests/web_test_helpers";
import { relayIcon } from "@incubacloud/utils/icons";
import { PasswordInput } from "@incubacloud/components/password_input/password_input";

// The hint and the placeholder are translated, read here as text.
beforeEach(() => allowTranslations());

describe("PasswordInput — class structure", () => {
    test("template name is set", () => {
        expect(PasswordInput.template).toBe("incubacloud.PasswordInput");
    });

    test("props defines hasValue as Boolean", () => {
        expect(PasswordInput.props.hasValue.type).toBe(Boolean);
    });

    test("props does not expose model, recordId or fieldName", () => {
        expect(PasswordInput.props.model).toBe(undefined);
        expect(PasswordInput.props.recordId).toBe(undefined);
        expect(PasswordInput.props.fieldName).toBe(undefined);
    });

    test("props defines onChange as Function", () => {
        expect(PasswordInput.props.onChange.type).toBe(Function);
    });

    test("props takes an optional id, for the form's label", () => {
        expect(PasswordInput.props.id).toEqual({ type: String, optional: true });
    });

    test("props takes the form's value, which the input shows", () => {
        expect(PasswordInput.props.value).toEqual({ type: String, optional: true });
    });

    test("props takes autoGenerate, for the fields the server fills when left blank", () => {
        expect(PasswordInput.props.autoGenerate).toEqual({ type: Boolean, optional: true });
    });

    test("reveal method does not exist", () => {
        expect(PasswordInput.prototype.reveal).toBe(undefined);
    });
});

/**
 * A PasswordInput stand-in with *props* and *state*, for its getters.
 *
 * @param {object} props the component's props
 * @param {object} state its state
 * @returns {object} the stand-in
 */
function standIn(props, state) {
    const comp = Object.create(PasswordInput.prototype);
    comp.props = props;
    comp.state = { replacing: false, ...state };
    comp.uid = "ic-password-0";
    return comp;
}

describe("PasswordInput — what it shows", () => {
    test("a stored value shows as Stored until it is replaced or the form holds a new one", () => {
        expect(standIn({ hasValue: true, value: "" }, {}).showsStored).toBe(true);
        expect(standIn({ hasValue: true }, {}).showsStored).toBe(true);
        expect(standIn({ hasValue: true, value: "" }, { replacing: true }).showsStored).toBe(false);
        expect(standIn({ hasValue: true, value: "new" }, {}).showsStored).toBe(false);
        expect(standIn({ hasValue: false, value: "" }, {}).showsStored).toBe(false);
    });

    test("the input says Not set when nothing is stored, New value over a stored one", () => {
        expect(standIn({ hasValue: false }, {}).placeholder).toBe("Not set");
        expect(standIn({ hasValue: true }, { replacing: true }).placeholder).toBe("New value");
    });

    test("the hint: the empty one when nothing is stored, none over a stored one", () => {
        expect(standIn({ hasValue: false, autoGenerate: true }, {}).hint).toBe(
            "Leave blank to auto-generate a strong password"
        );
        expect(standIn({ hasValue: false, emptyHint: "Paste the secret." }, {}).hint).toBe(
            "Paste the secret."
        );
        expect(standIn({ hasValue: true }, { replacing: true }).hint).toBe("");
        expect(standIn({ hasValue: true, value: "x" }, {}).hint).toInclude("will be saved");
    });

    test("auto-generate is promised only where the server generates one", () => {
        // An SMTP password or an S3 secret left blank stays empty.
        expect(standIn({ hasValue: false }, {}).hint).toBe("");
        expect(standIn({ hasValue: false, value: "" }, {}).hintId).toBe(undefined);
    });

    test("the hint describes the input, by the field's id or one of its own", () => {
        expect(standIn({ hasValue: false, autoGenerate: true, id: "pw" }, {}).hintId).toBe(
            "pw-hint"
        );
        expect(standIn({ hasValue: false, autoGenerate: true }, {}).hintId).toBe(
            "ic-password-0-hint"
        );
        expect(standIn({ hasValue: true, id: "pw" }, { replacing: true }).hintId).toBe(undefined);
    });

    test("a row's error describes the input before the hint, and alone without one", () => {
        expect(
            standIn(
                { hasValue: false, autoGenerate: true, id: "pw", ariaDescribedby: "pw-err" }, {},
            ).inputDescribedby
        ).toBe("pw-err pw-hint");
        expect(standIn({ hasValue: false, id: "pw", ariaDescribedby: "pw-err" }, {}).inputDescribedby)
            .toBe("pw-err");
        expect(standIn({ hasValue: false, id: "pw" }, {}).inputDescribedby).toBe(undefined);
    });
});

/**
 * A form row holding a PasswordInput under its label, as the panel's
 * forms write it: the label names the field by its id, and the form
 * holds the value, which a section shown again (``state.shown``) gives
 * back to the field.
 */
class Row extends Component {
    static template = xml`
        <div class="rl-frow">
            <label for="pw" id="pw-label">Client secret</label>
            <PasswordInput t-if="state.shown" id="'pw'" value="state.value"
                           hasValue="props.hasValue" autoGenerate="props.autoGenerate"
                           onChange="(value) => this.onChange(value)"/>
        </div>`;
    static components = { PasswordInput };
    static props = { hasValue: Boolean, autoGenerate: Boolean, onChange: Function };

    /** Hold the form's value and whether the field is shown. */
    setup() {
        this.state = useState({ value: "", shown: true });
    }

    /**
     * Keep what the field sends, as a form does, and report it.
     *
     * @param {string} value the field's new value
     */
    onChange(value) {
        this.state.value = value;
        this.props.onChange(value);
    }
}

/**
 * Mount a Row and return the values its field sends to the form.
 *
 * @param {boolean} hasValue whether a value is stored
 * @param {boolean} [autoGenerate] whether the server generates one left blank
 * @returns {Promise<{sent: string[], row: Row}>} each value onChange
 *     received, in order, and the row
 */
async function mountRow(hasValue, autoGenerate = false) {
    const sent = [];
    const row = await mountWithCleanup(Row, {
        props: { hasValue, autoGenerate, onChange: (value) => sent.push(value) },
        env: { icon: relayIcon },
        noMainContainer: true,
    });
    await waitFor(".ic-password-input");
    return { sent, row };
}

describe("PasswordInput — mounted", () => {
    test("nothing stored: an empty input that says Not set, named by the label", async () => {
        await mountRow(false, true);
        expect(".ic-password-stored").toHaveCount(0);
        expect("input#pw").toHaveAttribute("placeholder", "Not set");
        expect("input#pw").toHaveValue("");
        expect("input#pw").toHaveAttribute("aria-describedby", "pw-hint");
        expect("#pw-hint").toHaveText("Leave blank to auto-generate a strong password");
        expect(".ic-password-input button:contains(Keep stored)").toHaveCount(0);
    });

    test("nothing stored, nothing generated: no hint that promises one", async () => {
        await mountRow(false);
        expect("input#pw").toHaveValue("");
        expect(".ic-password-hint").toHaveCount(0);
        expect("input#pw").not.toHaveAttribute("aria-describedby");
    });

    test("Stored, Replace, a new value typed: the form gets it to save", async () => {
        const { sent } = await mountRow(true);
        // Stored, with a lock and Replace; no input, nothing sent.
        expect(".ic-password-stored").toHaveText(/Stored/);
        expect(".ic-password-stored svg").toHaveCount(1);
        expect("input").toHaveCount(0);
        // Replace keeps the name its text gives; the label describes it,
        // and its for names no control while there is no input, so
        // pressing it does not press Replace.
        const replace = queryOne(".ic-password-stored button");
        expect(replace).toHaveAttribute("id", "pw-replace");
        expect("#pw").toHaveCount(0);
        expect(replace).toHaveAttribute("aria-label", "Replace");
        expect(replace).toHaveAttribute("aria-describedby", "pw-label");

        await click(".ic-password-stored button");
        await animationFrame();
        expect(".ic-password-stored").toHaveCount(0);
        expect("input#pw").toHaveAttribute("placeholder", "New value");
        expect("input#pw").toBeFocused();
        expect(sent).toEqual([]);

        await edit("s3cret-new");
        await animationFrame();
        expect(sent.at(-1)).toBe("s3cret-new");
        expect("#pw-hint").toHaveText("New value entered — will be saved");
    });

    test("Keep stored drops what was typed and gives the focus back to Replace", async () => {
        const { sent } = await mountRow(true);
        await click(".ic-password-stored button");
        await animationFrame();
        await edit("typed-by-mistake");
        await animationFrame();

        await click(".ic-password-input button:contains(Keep stored)");
        await animationFrame();
        // The form holds "" again, which keeps the stored value.
        expect(sent.at(-1)).toBe("");
        expect("input").toHaveCount(0);
        expect(".ic-password-stored button").toBeFocused();
    });

    test("shown again, the field shows the value its form holds, not Stored", async () => {
        const { sent, row } = await mountRow(true);
        await click(".ic-password-stored button");
        await animationFrame();
        await edit("typed-before-the-tab-changed");
        await animationFrame();

        // Another tab, then this one again: the field is mounted anew.
        row.state.shown = false;
        await animationFrame();
        expect(".ic-password-input").toHaveCount(0);
        row.state.shown = true;
        await animationFrame();

        expect(".ic-password-stored").toHaveCount(0);
        expect("input#pw").toHaveValue("typed-before-the-tab-changed");
        expect("#pw-hint").toHaveText("New value entered — will be saved");
        expect(".ic-password-input button:contains(Keep stored)").toHaveCount(1);
        expect(sent.at(-1)).toBe("typed-before-the-tab-changed");

        // Cleared there, it stays open on the empty input, as after Replace.
        await click("input#pw");
        await edit("");
        await animationFrame();
        expect("input#pw").toHaveCount(1);
        expect(sent.at(-1)).toBe("");
    });
});
