import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { patchTranslations } from "@web/../tests/web_test_helpers";
import { RlSelect } from "@incubacloud/components/rl_select/rl_select";

/**
 * RlSelect — where its fixed menu opens relative to the trigger.
 *
 * No mounting (see ic_confirm_dialog.test.js): the shipped geometry methods
 * run on a stand-in holding the props, the trigger's rectangle and the
 * viewport height they read.
 */

/**
 * A select stand-in whose trigger sits at *top*..*bottom* in a viewport of
 * *viewport* px.
 *
 * @param {number} top trigger's top edge
 * @param {number} bottom trigger's bottom edge
 * @param {number} viewport viewport height
 * @param {number} [count] how many options it has
 * @returns {object} the stand-in
 */
function select(top, bottom, viewport, count = 3) {
    const comp = Object.create(RlSelect.prototype);
    comp.props = {
        options: Array.from({ length: count }, (_, i) => `option ${i}`),
        value: "",
        onChange() {},
    };
    comp.wrapRef = {
        el: { getBoundingClientRect: () => ({ top, bottom, left: 40, width: 200 }) },
    };
    comp._viewportHeight = () => viewport;
    return comp;
}

/** The numeric value of *property* in an inline style, or null when absent. */
function px(style, property) {
    const match = style.match(new RegExp(`(?:^|;)${property}:(-?\\d+)px`));
    return match ? Number(match[1]) : null;
}

describe("RlSelect — menu placement", () => {
    beforeEach(() => patchTranslations());

    test("a trigger near the top opens its menu below, as before", () => {
        const style = select(100, 136, 720)._computeDropdownStyle();
        expect(style).toInclude("position:fixed");
        expect(px(style, "top")).toBe(140);
        expect(px(style, "bottom")).toBe(null);
        expect(style).toInclude("left:40px");
        expect(style).toInclude("min-width:200px");
    });

    test("a trigger at the foot of the window opens its menu upwards, inside it", () => {
        // 10 options need 280 px; 36 px of trigger ends 20 px from the bottom.
        const style = select(664, 700, 720, 10)._computeDropdownStyle();
        expect(px(style, "top")).toBe(null);
        // Anchored 4 px above the trigger's top edge: 720 - 664 + 4.
        expect(px(style, "bottom")).toBe(60);
        // The menu cannot grow past the window's top: 664 - 4 - 8 = 652, cap 280.
        expect(px(style, "max-block-size")).toBe(280);
    });

    test("the height is the space of the side chosen, with the cap", () => {
        const below = select(100, 136, 400, 10)._computeDropdownStyle();
        // Below: 400 - 136 - 4 - 8 = 252 < 280 needed, above: 100 - 12 = 88 is less.
        expect(px(below, "top")).toBe(140);
        expect(px(below, "max-block-size")).toBe(252);
    });

    test("with little room on both sides it stays on the larger one and limits the height", () => {
        // 400 px window, trigger nearer the foot: 212 px above it, 128 below.
        const style = select(224, 260, 400, 10)._computeDropdownStyle();
        expect(px(style, "bottom")).toBe(180);
        expect(px(style, "max-block-size")).toBe(212);
        // Nearer the top: it opens below with its space.
        const lower = select(140, 176, 400, 10)._computeDropdownStyle();
        expect(px(lower, "top")).toBe(180);
        expect(px(lower, "max-block-size")).toBe(212);
    });

    test("a short list that fits below stays below near the foot", () => {
        // 2 options need 83 px; 720 - 600 - 12 = 108 are free below.
        const style = select(564, 600, 720, 2)._computeDropdownStyle();
        expect(px(style, "top")).toBe(604);
        expect(px(style, "bottom")).toBe(null);
    });

    test("the height never drops under its minimum", () => {
        const style = select(30, 66, 90, 10)._computeDropdownStyle();
        expect(px(style, "max-block-size")).toBe(96);
    });

    test("the clear row counts in the height the menu needs", () => {
        const comp = select(0, 36, 720, 3);
        const plain = comp._menuHeight();
        comp.props.emptyLabel = "any";
        expect(comp._menuHeight()).toBe(plain + 35.5);
    });

    test("a trigger that is not mounted gives no style", () => {
        const comp = select(0, 36, 720);
        comp.wrapRef = { el: null };
        expect(comp._computeDropdownStyle()).toBe("");
    });
});
