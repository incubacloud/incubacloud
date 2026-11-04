import { describe, expect, test } from "@odoo/hoot";

import { IcMenu } from "@incubacloud/components/ic_menu/ic_menu";

describe("IcMenu keyboard navigation", () => {
    test("Arrow Down moves to the next entry and wraps", () => {
        expect(IcMenu.nextIndex(3, 0, "ArrowDown")).toBe(1);
        expect(IcMenu.nextIndex(3, 2, "ArrowDown")).toBe(0);
    });

    test("Arrow Down with nothing focused lands on the first", () => {
        expect(IcMenu.nextIndex(3, -1, "ArrowDown")).toBe(0);
    });

    test("Arrow Up moves back and wraps to the last", () => {
        expect(IcMenu.nextIndex(3, 2, "ArrowUp")).toBe(1);
        expect(IcMenu.nextIndex(3, 0, "ArrowUp")).toBe(2);
        expect(IcMenu.nextIndex(3, -1, "ArrowUp")).toBe(2);
    });

    test("Home and End go to the ends", () => {
        expect(IcMenu.nextIndex(4, 2, "Home")).toBe(0);
        expect(IcMenu.nextIndex(4, 1, "End")).toBe(3);
    });

    test("other keys and an empty menu do not navigate", () => {
        expect(IcMenu.nextIndex(3, 1, "a")).toBe(-1);
        expect(IcMenu.nextIndex(0, -1, "ArrowDown")).toBe(-1);
    });

    test("the label is a required prop", () => {
        expect(IcMenu.props.label.type).toBe(String);
        expect(IcMenu.props.label.optional).not.toBe(true);
    });
});
