import { describe, expect, test } from "@odoo/hoot";
import { click, press, waitFor } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, xml } from "@odoo/owl";
import { mountWithCleanup, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";

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

describe("IcMenu focus", () => {
    class Page extends Component {
        static template = xml`
            <button id="outside" type="button">outside</button>
            <IcMenu label="'More'">
                <button id="first" type="button">One</button>
                <button id="second" type="button">Two</button>
            </IcMenu>`;
        static components = { IcMenu };
        static props = {};
    }

    /** Mount the page and open the menu with the keyboard. */
    async function openMenu() {
        // The mounted page is not the panel: nothing it needs is on the
        // server, and the mail stack of the test env must not ask for it.
        patchWithCleanup(rpc, { _rpc: () => Promise.resolve(false) });
        await mountWithCleanup(Page, { noMainContainer: true });
        await click(".ic-menu-trigger");
        await animationFrame();
        await waitFor("#first:focus");
    }

    test("Esc closes the menu and focuses the trigger", async () => {
        await openMenu();
        await press("Escape");
        await animationFrame();
        expect(".ic-menu-list").not.toBeVisible();
        expect(".ic-menu-trigger").toBeFocused();
    });

    test("Tab closes the menu and leaves the focus on the trigger", async () => {
        await openMenu();
        await press("Tab");
        await animationFrame();
        expect(".ic-menu-list").not.toBeVisible();
        expect(".ic-menu-trigger").toBeFocused();
    });

    test("Enter on an entry closes the menu and focuses the trigger", async () => {
        await openMenu();
        await press("ArrowDown");
        await press("Enter");
        await animationFrame();
        expect(".ic-menu-list").not.toBeVisible();
        expect(".ic-menu-trigger").toBeFocused();
    });

    test("a click outside closes the menu", async () => {
        await openMenu();
        await click("#outside");
        await animationFrame();
        expect(".ic-menu-list").not.toBeVisible();
    });
});
