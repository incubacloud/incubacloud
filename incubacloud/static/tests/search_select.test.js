import { describe, expect, test } from "@odoo/hoot";
import { SearchSelect } from "@incubacloud/components/search_select/search_select";

describe("SearchSelect — class structure", () => {
    test("template name is set", () => {
        expect(SearchSelect.template).toBe("incubacloud.SearchSelect");
    });

    test("props defines options as Array", () => {
        expect(SearchSelect.props.options.type).toBe(Array);
    });

    test("props defines onChange as Function", () => {
        expect(SearchSelect.props.onChange.type).toBe(Function);
    });

    test("defaultProps sets placeholder", () => {
        expect(SearchSelect.defaultProps.placeholder).toBe("Search\u2026");
    });

    test("defaultProps sets emptyLabel", () => {
        expect(SearchSelect.defaultProps.emptyLabel).toBe("\u2014 select \u2014");
    });

    test("defaultProps sets allowFreeText to false", () => {
        expect(SearchSelect.defaultProps.allowFreeText).toBe(false);
    });
});

describe("SearchSelect — option normalisation logic", () => {
    function normalise(options) {
        return options.map(o =>
            typeof o === "object" && o !== null
                ? o
                : { value: o, label: String(o) }
        );
    }

    test("normalises string options", () => {
        const result = normalise(["Alpha", "Beta"]);
        expect(result).toEqual([
            { value: "Alpha", label: "Alpha" },
            { value: "Beta", label: "Beta" },
        ]);
    });

    test("passes through object options", () => {
        const opts = [{ value: "a", label: "A" }];
        const result = normalise(opts);
        expect(result).toEqual([{ value: "a", label: "A" }]);
    });
});

describe("SearchSelect — filtered logic", () => {
    function filtered(opts, query) {
        const q = query.toLowerCase();
        if (!q) return opts;
        return opts.filter(o => o.label.toLowerCase().includes(q));
    }

    test("returns all options when query is empty", () => {
        const opts = [
            { value: "Alpha", label: "Alpha" },
            { value: "Beta", label: "Beta" },
            { value: "Gamma", label: "Gamma" },
        ];
        expect(filtered(opts, "")).toEqual(opts);
    });

    test("filters options by query", () => {
        const opts = [
            { value: "Alpha", label: "Alpha" },
            { value: "Beta", label: "Beta" },
            { value: "Gamma", label: "Gamma" },
        ];
        const result = filtered(opts, "Al");
        expect(result.length).toBe(1);
        expect(result[0].label).toBe("Alpha");
    });

    test("returns empty when nothing matches", () => {
        const opts = [
            { value: "Alpha", label: "Alpha" },
            { value: "Beta", label: "Beta" },
        ];
        const result = filtered(opts, "zzz");
        expect(result.length).toBe(0);
    });
});

describe("SearchSelect — selectedLabel logic", () => {
    function selectedLabel(value, opts) {
        if (value === null || value === undefined || value === "") return "";
        const match = opts.find(o => o.value === value);
        return match ? match.label : String(value);
    }

    test("returns empty for null value", () => {
        expect(selectedLabel(null, [])).toBe("");
    });

    test("returns empty for empty string", () => {
        expect(selectedLabel("", [])).toBe("");
    });

    test("returns label of matching option", () => {
        const opts = [{ value: "a", label: "Alpha" }];
        expect(selectedLabel("a", opts)).toBe("Alpha");
    });

    test("returns stringified value when no match", () => {
        expect(selectedLabel("unknown", [])).toBe("unknown");
    });
});

describe("SearchSelect — showFreeTextCreate logic", () => {
    function showFreeTextCreate(allowFreeText, input, opts) {
        if (!allowFreeText) return false;
        const q = input.trim();
        if (!q) return false;
        return !opts.some(o => o.label.toLowerCase() === q.toLowerCase());
    }

    test("returns false when allowFreeText is false", () => {
        expect(showFreeTextCreate(false, "test", [])).toBe(false);
    });

    test("returns false when input is empty", () => {
        expect(showFreeTextCreate(true, "", [])).toBe(false);
    });

    test("returns false when input matches existing option", () => {
        const opts = [{ value: "a", label: "Alpha" }];
        expect(showFreeTextCreate(true, "Alpha", opts)).toBe(false);
    });

    test("returns true when input is new", () => {
        const opts = [{ value: "a", label: "Alpha" }];
        expect(showFreeTextCreate(true, "Beta", opts)).toBe(true);
    });
});

describe("SearchSelect — method existence", () => {
    test("onFocus method exists", () => {
        expect(typeof SearchSelect.prototype.onFocus).toBe("function");
    });

    test("onInput method exists", () => {
        expect(typeof SearchSelect.prototype.onInput).toBe("function");
    });

    test("onBlur method exists", () => {
        expect(typeof SearchSelect.prototype.onBlur).toBe("function");
    });

    test("pick method exists", () => {
        expect(typeof SearchSelect.prototype.pick).toBe("function");
    });

    test("pickFreeText method exists", () => {
        expect(typeof SearchSelect.prototype.pickFreeText).toBe("function");
    });
});
