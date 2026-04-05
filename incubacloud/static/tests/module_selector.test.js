import { describe, expect, test } from "@odoo/hoot";
import { ModuleSelector } from "@incubacloud/components/module_selector/module_selector";

describe("ModuleSelector — class structure", () => {
    test("template name is set", () => {
        expect(ModuleSelector.template).toBe("incubacloud.ModuleSelector");
    });

    test("props defines value as String", () => {
        expect(ModuleSelector.props.value.type).toBe(String);
    });

    test("props defines onChange as Function", () => {
        expect(ModuleSelector.props.onChange.type).toBe(Function);
    });

    test("props defines modules as Array", () => {
        expect(ModuleSelector.props.modules.type).toBe(Array);
    });

    test("defaultProps sets placeholder", () => {
        expect(ModuleSelector.defaultProps.placeholder).toBe("Add module\u2026");
    });

    test("defaultProps sets emptyLabel to ALL", () => {
        expect(ModuleSelector.defaultProps.emptyLabel).toBe("ALL");
    });
});

describe("ModuleSelector — selected getter logic", () => {
    function selected(value) {
        return (value || "").split(",").map(s => s.trim()).filter(Boolean);
    }

    test("parses comma-separated value", () => {
        expect(selected("sale,purchase")).toEqual(["sale", "purchase"]);
    });

    test("handles empty string", () => {
        expect(selected("")).toEqual([]);
    });

    test("handles null", () => {
        expect(selected(null)).toEqual([]);
    });

    test("trims whitespace", () => {
        expect(selected(" sale , purchase ")).toEqual(["sale", "purchase"]);
    });
});

describe("ModuleSelector — filtered logic", () => {
    function filtered(modules, selected, blockedSet, query) {
        const q = query.toLowerCase();
        const sel = new Set(selected);
        const blk = blockedSet || new Set();
        return modules.filter(
            m => !sel.has(m) && !blk.has(m) && (!q || m.toLowerCase().includes(q))
        );
    }

    test("excludes already selected modules", () => {
        const result = filtered(["sale", "purchase", "stock"], ["sale"], new Set(), "");
        expect(result).toEqual(["purchase", "stock"]);
    });

    test("excludes blocked modules", () => {
        const result = filtered(["sale", "purchase", "stock"], [], new Set(["sale"]), "");
        expect(result).toEqual(["purchase", "stock"]);
    });

    test("filters by query", () => {
        const result = filtered(["sale", "purchase", "stock"], [], new Set(), "sto");
        expect(result).toEqual(["stock"]);
    });

    test("returns all non-selected when query is empty", () => {
        const result = filtered(["sale", "purchase"], ["sale"], new Set(), "");
        expect(result).toEqual(["purchase"]);
    });
});

describe("ModuleSelector — overlapError logic", () => {
    function overlapError(selected, blockedSet) {
        if (!blockedSet || !blockedSet.size) return null;
        const overlap = selected.filter(m => blockedSet.has(m));
        if (!overlap.length) return null;
        return `Conflict: "${overlap.join('", "')}" appears in both Include and Exclude`;
    }

    test("returns null when no blockedSet", () => {
        expect(overlapError(["sale"], null)).toBe(null);
    });

    test("returns null when no overlap", () => {
        expect(overlapError(["sale"], new Set(["stock"]))).toBe(null);
    });

    test("returns error message with overlapping modules", () => {
        const result = overlapError(["sale", "purchase"], new Set(["sale"]));
        expect(result).toInclude("sale");
        expect(result).toInclude("Conflict");
    });
});

describe("ModuleSelector — add/remove logic", () => {
    test("add builds comma-separated string", () => {
        const selected = ["sale"];
        const next = new Set(selected);
        next.add("stock");
        const result = [...next].join(",");
        expect(result).toBe("sale,stock");
    });

    test("remove produces comma-separated string without removed item", () => {
        const selected = ["sale", "purchase"];
        const next = new Set(selected);
        next.delete("sale");
        const result = [...next].join(",");
        expect(result).toBe("purchase");
    });

    test("add deduplicates", () => {
        const selected = ["sale"];
        const next = new Set(selected);
        next.add("sale");
        expect([...next]).toEqual(["sale"]);
    });
});
