import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, press, queryAll, queryOne, waitFor } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, useState, xml } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";

import { TabBar } from "@incubacloud/components/tab_bar/tab_bar";
import { FIELD_TABS, InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";

/**
 * The tab bar of a record's page and its «More ▾» (form standard §1): at
 * most eight tabs in the bar, the rest in a list the button opens, beside
 * the bar so that the bar's own scroll does not clip it.
 */

const BAR = [
    { key: "overview", label: "Overview" },
    { key: "general", label: "General" },
    { key: "networking", label: "Networking" },
];
const MORE = [
    { key: "deps", label: "Dependencies" },
    { key: "build", label: "Build" },
    { key: "database", label: "Database" },
];

/** A page holding the bar, as the instance's page does. */
class Page extends Component {
    static template = xml`
        <TabBar tabs="props.tabs" more="props.more" active="state.tab"
                marked="state.marked" onSelect="(key) => this.select(key)"/>`;
    static components = { TabBar };
    static props = ["tabs", "more", "chosen"];

    /** The open tab and the marked ones, as the page keeps them. */
    setup() {
        this.state = useState({ tab: "general", marked: [] });
        Page.current = this;
    }

    /**
     * Open a tab and note it.
     *
     * @param {string} key
     */
    select(key) {
        this.props.chosen.push(key);
        this.state.tab = key;
    }
}

/**
 * Mount the page with the bar.
 *
 * @param {object} [props]
 * @returns {Promise<string[]>} the keys chosen, in order
 */
async function mountBar({ tabs = BAR, more = MORE } = {}) {
    // Nothing of the page is on the server; the test env's mail stack
    // must not ask it either.
    patchWithCleanup(rpc, { _rpc: () => Promise.resolve(false) });
    const chosen = [];
    await mountWithCleanup(Page, { props: { tabs, more, chosen }, noMainContainer: true });
    return chosen;
}

describe("TabBar «More»", () => {
    beforeEach(() => allowTranslations());

    test("the bar shows its tabs and the button, the list closed", async () => {
        await mountBar();
        expect(queryAll(".rl-tabs > button.rl-tab[role=tab]:not(.ic-tabbar__more)")
            .map((el) => el.textContent.trim()))
            .toEqual(["Overview", "General", "Networking"]);
        expect(".ic-tabbar__more").toHaveText(/^More\s*▾$/);
        expect(".ic-tabbar__more").toHaveAttribute("role", "tab");
        expect(".ic-tabbar__more").toHaveAttribute("aria-selected", "false");
        expect(".ic-tabbar__more").toHaveAttribute("aria-expanded", "false");
        expect(".ic-tabbar__more").not.toHaveAttribute("aria-haspopup");
        expect(".ic-tabbar__more").not.toHaveAttribute("aria-controls");
        expect(".ic-tabbar__menu").toHaveCount(0);
    });

    test("opens beside the bar, with its entries as tabs", async () => {
        await mountBar();
        await click(".ic-tabbar__more");
        await animationFrame();
        expect(".ic-tabbar__more").toHaveAttribute("aria-expanded", "true");
        expect(".ic-tabbar__more").toHaveAttribute("aria-controls", queryOne(".ic-tabbar__menu").id);
        expect(".rl-tabs .ic-tabbar__menu").toHaveCount(0);
        expect(".ic-tabbar > .ic-tabbar__menu").toHaveCount(1);
        expect(queryAll(".ic-tabbar__menu button.rl-tab[role=tab]").map((el) => el.textContent.trim()))
            .toEqual(["Dependencies", "Build", "Database"]);
        await waitFor(".ic-tabbar__menu button.rl-tab:focus");
    });

    test("choosing an entry opens it and the button takes its name", async () => {
        const chosen = await mountBar();
        await click(".ic-tabbar__more");
        await animationFrame();
        await click(".ic-tabbar__menu button.rl-tab:contains(Database)");
        await animationFrame();
        expect(chosen).toEqual(["database"]);
        expect(".ic-tabbar__menu").toHaveCount(0);
        expect(".ic-tabbar__more").toHaveClass("active");
        expect(".ic-tabbar__more").toHaveAttribute("aria-selected", "true");
        expect(".ic-tabbar__more").toHaveText(/^Database\s*▾$/);
        expect(".rl-tabs > button.rl-tab.active").toHaveCount(1);
        expect(".ic-tabbar__more").toBeFocused();
    });

    test("a change under «More» marks the button and its entry", async () => {
        await mountBar();
        expect(".ic-tabbar__more .ic-tabbar__dot").toHaveCount(0);
        Page.current.state.marked = ["build"];
        await animationFrame();
        expect(".ic-tabbar__more .ic-tabbar__dot").toHaveCount(1);
        expect(queryOne(".ic-tabbar__more .ic-sr-only")).toHaveText("Unsaved changes");
        await click(".ic-tabbar__more");
        await animationFrame();
        expect(".ic-tabbar__menu button.rl-tab:contains(Build) .ic-tabbar__dot").toHaveCount(1);
        expect(".ic-tabbar__menu button.rl-tab:contains(Database) .ic-tabbar__dot").toHaveCount(0);
    });

    test("a change in a tab of the bar marks that tab, not the button", async () => {
        await mountBar();
        Page.current.state.marked = ["networking"];
        await animationFrame();
        expect(".rl-tabs > button.rl-tab:contains(Networking) .ic-tabbar__dot").toHaveCount(1);
        expect(".ic-tabbar__more .ic-tabbar__dot").toHaveCount(0);
    });

    test("Esc closes the list and gives the focus back to the button", async () => {
        await mountBar();
        await click(".ic-tabbar__more");
        await animationFrame();
        await waitFor(".ic-tabbar__menu button.rl-tab:focus");
        await press("Escape");
        await animationFrame();
        expect(".ic-tabbar__menu").toHaveCount(0);
        expect(".ic-tabbar__more").toBeFocused();
    });

    test("a press outside closes the list", async () => {
        await mountBar();
        await click(".ic-tabbar__more");
        await animationFrame();
        await click(".rl-tabs > button.rl-tab:contains(Overview)");
        await animationFrame();
        expect(".ic-tabbar__menu").toHaveCount(0);
    });

    test("a list of one goes in the bar, with no button", async () => {
        await mountBar({ more: [{ key: "activity", label: "Audit Logs" }] });
        expect(".ic-tabbar__more").toHaveCount(0);
        expect(".rl-tabs > button.rl-tab:contains(Audit Logs)").toHaveCount(1);
    });
});

describe("TabBar.place", () => {
    const wrap = { left: 100, top: 50, width: 600 };

    test("under the button, from its left edge", () => {
        expect(TabBar.place({
            button: { left: 300, bottom: 90 }, wrap, width: 190, viewport: 1440,
        })).toEqual({ left: 200, top: 44 });
    });

    test("pushed back so as not to pass the bar's end", () => {
        expect(TabBar.place({
            button: { left: 640, bottom: 90 }, wrap, width: 190, viewport: 1440,
        })).toEqual({ left: 410, top: 44 });
    });

    test("nor the viewport's", () => {
        expect(TabBar.place({
            button: { left: 300, bottom: 90 }, wrap: { left: 16, top: 50, width: 900 },
            width: 190, viewport: 390,
        })).toEqual({ left: 176, top: 44 });
    });
});

describe("InstanceDetail — tabs and marks", () => {
    beforeEach(() => allowTranslations());

    /**
     * A stand-in of the page on the shipped getters.
     *
     * @param {object} opts
     * @returns {object}
     */
    function page({ environment = "production", deployed = true, perms = {}, create = false } = {}) {
        const comp = Object.create(InstanceDetail.prototype);
        comp.props = create ? { project_id: 1 } : { instance_id: 7, project_id: 1 };
        comp.env = {
            permissions: {
                can_edit_instance: true, can_manage_backups: true, can_view_logs: true,
                can_view_metrics: true, can_manage_hosts: true, ...perms,
            },
        };
        comp.state = {
            form: { environment, name: "prod", smtp_relay_port: 587, auto_rebuild: false },
            inst: create ? null : { environment, deployed },
            selectedTags: [],
            pipDeps: "",
            aptDeps: "",
        };
        comp.validator = { fieldError: () => null };
        comp._savedForm = JSON.stringify(comp._editedValues());
        return comp;
    }

    const keys = (list) => list.map((t) => t.key);

    test("a production: eight in the bar, Email in Mails' place", () => {
        const { tabs, more } = page().instanceTabs;
        expect(keys(tabs)).toEqual([
            "overview", "general", "networking", "email", "repos", "config", "backups", "metrics",
        ]);
        expect(keys(more)).toEqual(["deps", "build", "resources", "database", "activity"]);
        expect(tabs.find((t) => t.key === "config").label).toBe("Odoo Config");
    });

    test("a staging: eight in the bar, Mails in Email's place, Whitelist under «More»", () => {
        const { tabs, more } = page({ environment: "staging" }).instanceTabs;
        expect(keys(tabs)).toEqual([
            "overview", "general", "networking", "repos", "config", "backups", "mails", "metrics",
        ]);
        expect(keys(more)).toEqual(["deps", "build", "resources", "database", "whitelist", "activity"]);
    });

    test("a production not deployed yet keeps Backups, to choose where they go", () => {
        expect(keys(page({ deployed: false }).instanceTabs.tabs).includes("backups")).toBe(true);
        expect(keys(page({ environment: "staging", deployed: false }).instanceTabs.tabs)
            .includes("backups")).toBe(false);
    });

    test("the new-instance form has no «More»", () => {
        const { tabs, more } = page({ create: true }).instanceTabs;
        expect(keys(tabs)).toEqual([
            "general", "database", "networking", "email", "repos", "deps", "resources", "backups",
        ]);
        expect(more).toEqual([]);
    });

    test("a change marks the tab that holds the field", () => {
        const comp = page();
        expect(comp.markedTabs).toEqual([]);
        comp.state.form.auto_rebuild = true;
        expect(comp.markedTabs).toEqual(["build"]);
    });

    test("tags mark General and dependencies mark Dependencies, as the save bar counts them", () => {
        const comp = page();
        comp.state.selectedTags = [{ id: 3, name: "client" }];
        expect(comp.markedTabs).toEqual(["general"]);
        comp.state.pipDeps = "requests";
        expect(comp.markedTabs).toEqual(["general", "deps"]);
        expect(comp.unsavedCount).toBe(2);
        comp.state.selectedTags = [];
        comp.state.pipDeps = "";
        comp.state.aptDeps = "curl";
        expect(comp.markedTabs).toEqual(["deps"]);
        expect(comp.unsavedCount).toBe(1);
    });

    test("a field to fix marks its tab too", () => {
        const comp = page();
        comp.validator = { fieldError: (f) => (f === "smtp_relay_port" ? "bad" : null) };
        expect(comp.markedTabs).toEqual(["email"]);
    });

    test("every field the save bar counts maps to a tab of the page", () => {
        // What a loaded instance's save bar counts: the form, the tags and
        // the dependencies. Only the fields no tab edits have no tab.
        const comp = page();
        comp.state.versionFacts = {};
        comp._applyInstanceData({ name: "prod", environment: "production", odoo_version: "19.0" }, [], []);
        const unmapped = Object.keys(comp._editedValues()).filter((f) => !(f in FIELD_TABS));
        expect(unmapped.sort()).toEqual([
            "environment", "odoo_initial_lang", "odoo_proxy", "odoo_version", "postgres_dbname",
        ]);
        const all = new Set([
            ...keys(page().instanceTabs.tabs), ...keys(page().instanceTabs.more),
            ...keys(page({ environment: "staging" }).instanceTabs.more),
        ]);
        for (const tab of Object.values(FIELD_TABS)) {
            expect(all.has(tab)).toBe(true);
        }
    });
});
