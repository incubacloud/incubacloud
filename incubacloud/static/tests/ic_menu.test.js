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

describe("IcMenu placement", () => {
    test("opens to the right of the trigger when it fits", () => {
        expect(IcMenu.place({ triggerLeft: 40, triggerRight: 110, width: 220, viewport: 768 }))
            .toEqual({ left: 40, maxWidth: 736 });
    });

    test("opens to the left when the right has no room", () => {
        // English at 768: the trigger near the right edge.
        expect(IcMenu.place({ triggerLeft: 680, triggerRight: 750, width: 220, viewport: 768 }))
            .toEqual({ left: 530, maxWidth: 736 });
    });

    test("is pushed back inside when neither side has room", () => {
        // The trigger wrapped to the left edge of a narrow screen, with a
        // list wider than what lies on either side of it.
        const { left } = IcMenu.place({ triggerLeft: 36, triggerRight: 110, width: 300, viewport: 320 });
        expect(left).toBe(16);
        const right = IcMenu.place({ triggerLeft: 250, triggerRight: 300, width: 280, viewport: 320 });
        expect(right.left).toBe(20);
        expect(right.left + 280).toBeLessThan(320 - 15);
    });

    test("is never wider than the viewport less its margins", () => {
        expect(IcMenu.place({ triggerLeft: 36, triggerRight: 110, width: 900, viewport: 390 }))
            .toEqual({ left: 16, maxWidth: 358 });
    });

    // The panel's styles are not in the test bundle: the list is laid
    // out here as ``ic_menu.scss`` lays it out.
    class EdgePage extends Component {
        static template = xml`
            <style>
                .ic-menu { position: relative; display: inline-flex; }
                .ic-menu .ic-menu-list { position: absolute; top: 100%; left: 0; min-width: 220px; display: grid; }
                .ic-menu .ic-menu-list > button { white-space: nowrap; }
                .ic-menu .ic-menu-list[hidden] { display: none; }
            </style>
            <div t-att-style="'position:fixed; top:10px; ' + props.side + ':4px'">
                <IcMenu label="'More'">
                    <button type="button">Import an existing instance from this host</button>
                    <button type="button">Traefik proxy settings</button>
                </IcMenu>
            </div>`;
        static components = { IcMenu };
        static props = { side: String };
    }

    for (const side of ["left", "right"]) {
        test(`a trigger at the ${side} edge opens a list inside the viewport`, async () => {
            patchWithCleanup(rpc, { _rpc: () => Promise.resolve(false) });
            await mountWithCleanup(EdgePage, { noMainContainer: true, props: { side } });
            await click(".ic-menu-trigger");
            await animationFrame();
            const el = document.querySelector(".ic-menu-list");
            const list = el.getBoundingClientRect();
            const viewport = document.documentElement.clientWidth;
            expect(list.left).toBeGreaterThan(15);
            expect(list.right).toBeLessThan(viewport - 15);
        });
    }
});
