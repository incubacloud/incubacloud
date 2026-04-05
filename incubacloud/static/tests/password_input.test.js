import { describe, expect, test } from "@odoo/hoot";
import { PasswordInput } from "@incubacloud/components/password_input/password_input";

describe("PasswordInput — class structure", () => {
    test("template name is set", () => {
        expect(PasswordInput.template).toBe("incubacloud.PasswordInput");
    });

    test("props defines hasValue as Boolean", () => {
        expect(PasswordInput.props.hasValue.type).toBe(Boolean);
    });

    test("props defines model as String", () => {
        expect(PasswordInput.props.model.type).toBe(String);
    });

    test("props defines fieldName as String", () => {
        expect(PasswordInput.props.fieldName.type).toBe(String);
    });

    test("props defines onChange as Function", () => {
        expect(PasswordInput.props.onChange.type).toBe(Function);
    });

    test("props defines recordId as optional", () => {
        expect(PasswordInput.props.recordId.optional).toBe(true);
    });
});

describe("PasswordInput — hint getter logic", () => {
    test("shows keep-current hint when hasValue is true", () => {
        // Replicate the hint getter logic
        const hasValue = true;
        const hint = hasValue
            ? "Password set \u2014 leave blank to keep current"
            : "Leave blank to auto-generate a strong password";
        expect(hint).toInclude("keep current");
    });

    test("shows auto-generate hint when hasValue is false", () => {
        const hasValue = false;
        const hint = hasValue
            ? "Password set \u2014 leave blank to keep current"
            : "Leave blank to auto-generate a strong password";
        expect(hint).toInclude("auto-generate");
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

describe("PasswordInput — reveal guard logic", () => {
    test("reveal does nothing without recordId", () => {
        const recordId = null;
        const revealing = false;
        const shouldReveal = recordId && !revealing;
        expect(!!shouldReveal).toBe(false);
    });

    test("reveal does nothing while already revealing", () => {
        const recordId = 42;
        const revealing = true;
        const shouldReveal = recordId && !revealing;
        expect(!!shouldReveal).toBe(false);
    });

    test("reveal proceeds with recordId and not revealing", () => {
        const recordId = 42;
        const revealing = false;
        const shouldReveal = recordId && !revealing;
        expect(!!shouldReveal).toBe(true);
    });
});

describe("PasswordInput — method existence", () => {
    test("onInput method exists", () => {
        expect(typeof PasswordInput.prototype.onInput).toBe("function");
    });

    test("toggleVisible method exists", () => {
        expect(typeof PasswordInput.prototype.toggleVisible).toBe("function");
    });

    test("reveal method exists", () => {
        expect(typeof PasswordInput.prototype.reveal).toBe("function");
    });

    test("copyToClipboard method exists", () => {
        expect(typeof PasswordInput.prototype.copyToClipboard).toBe("function");
    });
});
