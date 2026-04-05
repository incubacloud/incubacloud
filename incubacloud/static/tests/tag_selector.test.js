import { describe, expect, test } from "@odoo/hoot";
import { TagSelector, TAG_COLORS, tagStyle, tagDotStyle } from "@incubacloud/components/tag_selector/tag_selector";

const ALL_TAGS = [
    { id: 1, name: "frontend", color: 0 },
    { id: 2, name: "backend", color: 1 },
    { id: 3, name: "devops", color: 2 },
];

describe("TagSelector — class structure", () => {
    test("template name is set", () => {
        expect(TagSelector.template).toBe("incubacloud.TagSelector");
    });

    test("props defines selected as Array", () => {
        expect(TagSelector.props.selected.type).toBe(Array);
    });

    test("props defines available as Array", () => {
        expect(TagSelector.props.available.type).toBe(Array);
    });

    test("props defines onAdd as Function", () => {
        expect(TagSelector.props.onAdd.type).toBe(Function);
    });

    test("props defines onRemove as Function", () => {
        expect(TagSelector.props.onRemove.type).toBe(Function);
    });

    test("props defines onCreate as Function", () => {
        expect(TagSelector.props.onCreate.type).toBe(Function);
    });
});

describe("TagSelector — filtered logic", () => {
    function filtered(available, selected, query) {
        const q = query.toLowerCase();
        const selectedIds = new Set(selected.map(t => t.id));
        return available.filter(
            t => !selectedIds.has(t.id) && (!q || t.name.toLowerCase().includes(q))
        );
    }

    test("excludes already selected tags", () => {
        const result = filtered(ALL_TAGS, [ALL_TAGS[0]], "");
        expect(result.length).toBe(2);
        expect(result[0].name).toBe("backend");
        expect(result[1].name).toBe("devops");
    });

    test("filters by query", () => {
        const result = filtered(ALL_TAGS, [], "back");
        expect(result.length).toBe(1);
        expect(result[0].name).toBe("backend");
    });

    test("returns all non-selected when query is empty", () => {
        const result = filtered(ALL_TAGS, [], "");
        expect(result.length).toBe(3);
    });

    test("returns empty when query matches nothing", () => {
        const result = filtered(ALL_TAGS, [], "zzz");
        expect(result.length).toBe(0);
    });
});

describe("TagSelector — showCreate logic", () => {
    function showCreate(available, input) {
        const q = input.trim();
        if (!q) return false;
        return !available.some(t => t.name.toLowerCase() === q.toLowerCase());
    }

    test("returns false for empty input", () => {
        expect(showCreate(ALL_TAGS, "")).toBe(false);
    });

    test("returns false when input matches existing tag", () => {
        expect(showCreate(ALL_TAGS, "frontend")).toBe(false);
    });

    test("returns true when input is new", () => {
        expect(showCreate(ALL_TAGS, "newstuff")).toBe(true);
    });

    test("is case insensitive", () => {
        expect(showCreate(ALL_TAGS, "Frontend")).toBe(false);
    });
});

describe("TagSelector — TAG_COLORS and style helpers", () => {
    test("TAG_COLORS has 10 entries", () => {
        expect(TAG_COLORS.length).toBe(10);
    });

    test("each color has bg and text", () => {
        for (const c of TAG_COLORS) {
            expect(typeof c.bg).toBe("string");
            expect(typeof c.text).toBe("string");
        }
    });

    test("tagStyle returns background and color", () => {
        const style = tagStyle({ color: 0 });
        expect(style).toInclude("background:");
        expect(style).toInclude("color:");
    });

    test("tagDotStyle returns background", () => {
        const style = tagDotStyle({ color: 0 });
        expect(style).toInclude("background:");
    });

    test("tagStyle wraps around for colors beyond palette size", () => {
        const style = tagStyle({ color: 10 });
        // color 10 % 10 = 0, should match color 0
        const style0 = tagStyle({ color: 0 });
        expect(style).toBe(style0);
    });
});

describe("TagSelector — method existence", () => {
    test("onInput method exists", () => {
        expect(typeof TagSelector.prototype.onInput).toBe("function");
    });

    test("onFocus method exists", () => {
        expect(typeof TagSelector.prototype.onFocus).toBe("function");
    });

    test("onBlur method exists", () => {
        expect(typeof TagSelector.prototype.onBlur).toBe("function");
    });

    test("selectTag method exists", () => {
        expect(typeof TagSelector.prototype.selectTag).toBe("function");
    });

    test("createTag method exists", () => {
        expect(typeof TagSelector.prototype.createTag).toBe("function");
    });
});
