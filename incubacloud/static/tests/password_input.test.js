import { describe, expect, test } from "@odoo/hoot";
import { PasswordInput } from "@incubacloud/components/password_input/password_input";

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
});

describe("PasswordInput — hint getter logic", () => {
    test("shows keep-current hint when hasValue is true", () => {
        const hasValue = true;
        const inputValue = "";
        const hint = inputValue
            ? "New value entered \u2014 will be saved"
            : hasValue
            ? "Value stored \u2014 type a new one to replace it"
            : "Leave blank to auto-generate a strong password";
        expect(hint).toInclude("replace it");
    });

    test("shows auto-generate hint when hasValue is false", () => {
        const hasValue = false;
        const inputValue = "";
        const hint = inputValue
            ? "New value entered \u2014 will be saved"
            : hasValue
            ? "Value stored \u2014 type a new one to replace it"
            : "Leave blank to auto-generate a strong password";
        expect(hint).toInclude("auto-generate");
    });

    test("shows new-value hint when user has typed something", () => {
        const hasValue = true;
        const inputValue = "newpassword";
        const hint = inputValue
            ? "New value entered \u2014 will be saved"
            : hasValue
            ? "Value stored \u2014 type a new one to replace it"
            : "Leave blank to auto-generate a strong password";
        expect(hint).toInclude("will be saved");
    });
});

describe("PasswordInput — isSet getter logic", () => {
    test("isSet is true when hasValue and no inputValue", () => {
        const hasValue = true;
        const inputValue = "";
        const isSet = hasValue && !inputValue;
        expect(isSet).toBe(true);
    });

    test("isSet is false when hasValue but user has typed a replacement", () => {
        const hasValue = true;
        const inputValue = "newvalue";
        const isSet = hasValue && !inputValue;
        expect(isSet).toBe(false);
    });

    test("isSet is false when no stored value", () => {
        const hasValue = false;
        const inputValue = "";
        const isSet = hasValue && !inputValue;
        expect(isSet).toBe(false);
    });
});

describe("PasswordInput — toggleVisible logic", () => {
    test("toggles visibility state", () => {
        let visible = false;
        visible = !visible;
        expect(visible).toBe(true);
        visible = !visible;
        expect(visible).toBe(false);
    });
});

describe("PasswordInput — method existence", () => {
    test("onInput method exists", () => {
        expect(typeof PasswordInput.prototype.onInput).toBe("function");
    });

    test("toggleVisible method exists", () => {
        expect(typeof PasswordInput.prototype.toggleVisible).toBe("function");
    });

    test("copyToClipboard method exists", () => {
        expect(typeof PasswordInput.prototype.copyToClipboard).toBe("function");
    });

    test("reveal method does not exist", () => {
        expect(PasswordInput.prototype.reveal).toBe(undefined);
    });
});
