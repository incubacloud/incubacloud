import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations } from "@web/../tests/web_test_helpers";
import { MultiSelect } from "@incubacloud/components/multi_select/multi_select";

/**
 * MultiSelect — the generic searchable multi-select (Settings → Assistant's
 * models and users, 3-nov-2026).
 *
 * No mounting (see ic_confirm_dialog.test.js): the getters and handlers are
 * the shipped ones, on a stand-in holding the props and state they read.
 */

const OPTIONS = [
    { value: "opus", label: "Claude Opus", hint: "Recommended" },
    { value: "sonnet", label: "Claude Sonnet", hint: "Recommended" },
    { value: "new-x", label: "New X", hint: "Untested · Price unknown", disabled: true },
    { value: "haiku", label: "Claude Haiku", hint: "Untested" },
];

/**
 * A selector stand-in with *value* chosen; every change lands in ``changes``.
 *
 * @param {Array} value the values chosen
 * @param {object} [props] other props
 * @returns {object} the stand-in
 */
function selector(value, props = {}) {
    const comp = Object.create(MultiSelect.prototype);
    comp.changes = [];
    comp.props = {
        options: OPTIONS,
        value,
        onChange: (values) => {
            comp.changes.push(values);
            comp.props.value = values;
        },
        ...props,
    };
    comp.state = { input: "", open: false, active: 0, menuStyle: "" };
    comp.wrapRef = { el: null };
    comp.inputRef = { el: null };
    return comp;
}

/** A key press on the search field, as the handler reads it. */
function key(name) {
    return { key: name, preventDefault() {} };
}

describe("MultiSelect", () => {
    beforeEach(() => allowTranslations());

    test("is generic: options, values and a change callback", () => {
        expect(MultiSelect.template).toBe("incubacloud.MultiSelect");
        expect(MultiSelect.props.options.type).toBe(Array);
        expect(MultiSelect.props.value.type).toBe(Array);
        expect(MultiSelect.props.onChange.type).toBe(Function);
    });

    test("shows the chosen ones as chips, in the order picked", () => {
        const chips = selector(["sonnet", "opus"]).chips;
        expect(chips.map((chip) => chip.label)).toEqual(["Claude Sonnet", "Claude Opus"]);
    });

    test("a value no option has still shows, to be removed", () => {
        expect(selector(["gone"]).chips).toEqual([{ value: "gone", label: "gone", disabled: false }]);
    });

    test("the menu lists the others, filtered by label or hint", () => {
        const comp = selector(["opus"]);
        expect(comp.filtered.map((option) => option.value)).toEqual(["sonnet", "new-x", "haiku"]);
        comp.state.input = "untested";
        expect(comp.filtered.map((option) => option.value)).toEqual(["new-x", "haiku"]);
        comp.state.input = "HAIKU";
        expect(comp.filtered.map((option) => option.value)).toEqual(["haiku"]);
    });

    test("a pick joins the end, the menu stays open; a disabled one is not picked", () => {
        const comp = selector(["opus"]);
        comp.state.open = true;
        comp.state.input = "hai";
        comp.pick(OPTIONS[3]);
        expect(comp.props.value).toEqual(["opus", "haiku"]);
        expect(comp.state.input).toBe("");
        expect(comp.state.open).toBe(true);
        comp.pick(OPTIONS[2]);
        comp.pick(OPTIONS[0]);
        expect(comp.changes.length).toBe(1);
    });

    test("removing takes the value out", () => {
        const comp = selector(["opus", "sonnet"]);
        comp.remove("opus");
        expect(comp.props.value).toEqual(["sonnet"]);
    });

    test("disabled, nothing is picked, removed or opened", () => {
        const comp = selector(["opus"], { disabled: true });
        comp.pick(OPTIONS[1]);
        comp.remove("opus");
        comp.open();
        expect(comp.changes).toEqual([]);
        expect(comp.state.open).toBe(false);
    });

    test("arrows move through the menu and Enter picks", () => {
        const comp = selector([]);
        comp.onKeydown(key("ArrowDown"));
        expect(comp.state.open).toBe(true);
        comp.onKeydown(key("ArrowDown"));
        expect(comp.state.active).toBe(1);
        comp.onKeydown(key("ArrowUp"));
        comp.onKeydown(key("ArrowUp"));
        expect(comp.state.active).toBe(OPTIONS.length - 1);
        comp.onKeydown(key("Enter"));
        expect(comp.props.value).toEqual(["haiku"]);
    });

    test("Backspace in an empty field removes the last chip; Escape closes", () => {
        const comp = selector(["opus", "sonnet"]);
        comp.onKeydown(key("Backspace"));
        expect(comp.props.value).toEqual(["opus"]);
        comp.state.input = "x";
        comp.onKeydown(key("Backspace"));
        expect(comp.props.value).toEqual(["opus"]);
        comp.state.open = true;
        comp.onKeydown(key("Escape"));
        expect(comp.state.open).toBe(false);
        expect(comp.state.input).toBe("");
    });

    test("each chip's remove button names it", () => {
        expect(String(selector([]).removeLabel({ label: "Claude Opus" }))).toBe("Remove Claude Opus");
    });
});
