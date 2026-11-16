import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, queryAll, queryAllTexts, queryOne } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, xml } from "@odoo/owl";
import { allowTranslations, mountWithCleanup, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { getTemplate } from "@web/core/templates";
import {
    canReopenFirstSteps,
    FirstSteps,
    firstStepsStore,
    loadFirstSteps,
    registerFirstStepAction,
    registerFirstStepsHeader,
} from "@incubacloud/components/first_steps/first_steps";
import { ProjectDashboard } from "@incubacloud/components/project_dashboard/project_dashboard";
import { _buildUrl, _parseRoute } from "@incubacloud/app/app";
import { relayIcon } from "@incubacloud/utils/icons";

/**
 * The first steps card of the home page: what it draws of
 * ``/cloud/first_steps``, closing it, folding it, its actions and a
 * layer's header.
 */

/**
 * A state as ``/cloud/first_steps`` answers it.
 *
 * @param {object} over - what differs
 * @returns {object}
 */
function stateOf(over = {}) {
    return {
        enabled: true, visible: true, hidden: false, pending: true,
        collapsed: false, done: 0, required: 3, optional_left: 2, next: "server",
        header: {},
        steps: [
            { code: "server", title: "Get a server", help: "Odoo runs on a server of yours.",
              optional: false, state: "todo", failed: false,
              actions: [
                  { code: "connect_server", label: "Connect a server", route: "new_host", params: {} },
                  { code: "open_dialog", label: "Open a dialog", client: true },
              ],
              guide_url: "https://docs.incubacloud.io/hosts/connect/" },
            { code: "backups", title: "Set up backups", help: "Backups go to a destination.",
              optional: false, state: "started", failed: true,
              actions: [{ code: "new_backup_backend", label: "Add a backup destination",
                          route: "new_backup_backend", params: {} }],
              guide_url: "https://docs.incubacloud.io/backups/" },
            { code: "deploy", title: "Deploy Odoo", help: "A project holds your instances.",
              optional: false, state: "done", failed: false, actions: [], guide_url: "" },
            { code: "domain", title: "Use your own domain", help: "After your first deployment.",
              optional: true, state: "todo", failed: false, actions: [], guide_url: "" },
        ],
        ...over,
    };
}

async function mountCard(data, env = {}) {
    firstStepsStore.data = data;
    firstStepsStore.loaded = true;
    await mountWithCleanup(FirstSteps, {
        env: { icon: relayIcon, navigate: () => {}, ...env },
        noMainContainer: true,
    });
}

describe("FirstSteps — the card", () => {
    beforeEach(() => {
        allowTranslations();
        registerFirstStepsHeader(null);
    });

    test("a section named by its title, the steps in a list, the next one current", async () => {
        await mountCard(stateOf());
        expect("section.ic-fs").toHaveAttribute("aria-labelledby", "ic-fs-title");
        expect("#ic-fs-title").toHaveText("First steps");
        expect("ol.ic-fs-steps > li").toHaveCount(4);
        expect("li[aria-current=step]").toHaveCount(1);
        expect(queryOne("li[aria-current=step] .ic-fs-step-title").textContent).toInclude("Get a server");
        expect(".ic-fs-count").toHaveAttribute("aria-live", "polite");
        expect(".ic-fs-count").toHaveText("0 of 3 done");
    });

    test("the state in words, never by colour alone, and optional said", async () => {
        await mountCard(stateOf());
        expect(queryAllTexts(".ic-fs-state-text")).toEqual(["To do", "Started", "Done", "To do"]);
        expect(".ic-fs-optional").toHaveCount(1);
        expect(".ic-fs-optional").toHaveText("· optional");
        expect(".ic-fs-help--failed").toHaveCount(1);
    });

    test("only the next step's first button is primary; a done step has none", async () => {
        await mountCard(stateOf());
        expect(".rl-btn-primary").toHaveCount(1);
        expect(".rl-btn-primary").toHaveText("Connect a server");
        expect(queryAll(".ic-fs-step--done .ic-fs-action")).toHaveLength(0);
        expect(".ic-fs-guide").toHaveCount(2);
        expect(queryAll(".ic-fs-guide")[0]).toHaveAttribute("target", "_blank");
    });

    test("no button of the card says New Project: the page's header keeps it", () => {
        const text = getTemplate("incubacloud.FirstSteps").textContent;
        expect(text).not.toInclude("New Project");
    });

    test("a page action navigates, a client action runs what a layer registered", async () => {
        const navigated = [];
        const ran = [];
        registerFirstStepAction("open_dialog", (env, action) => ran.push(action.code));
        await mountCard(stateOf(), { navigate: (route, params) => navigated.push([route, params]) });
        await click(".ic-fs-action:contains(Connect a server)");
        await click(".ic-fs-action:contains(Open a dialog)");
        expect(navigated).toEqual([["new_host", {}]]);
        expect(ran).toEqual(["open_dialog"]);
    });

    test("× closes it for this user", async () => {
        const calls = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                calls.push([url, params]);
                return Promise.resolve(stateOf({ visible: false, hidden: true }));
            },
        });
        await mountCard(stateOf());
        expect("button.ic-fs-close").toHaveAttribute("aria-label", "Hide first steps");
        await click("button.ic-fs-close");
        await animationFrame();
        expect(calls).toEqual([["/cloud/first_steps/set_hidden", { hidden: true }]]);
        expect("section.ic-fs").toHaveCount(0);
    });

    test("with the required steps done it is one line that unfolds", async () => {
        await mountCard(stateOf({ collapsed: true, done: 3, next: "domain" }));
        expect("ol.ic-fs-steps").toHaveCount(0);
        expect("button.ic-fs-fold [aria-live]").toHaveText("First steps · 3 of 3 done · 2 optional left");
        expect("button.ic-fs-fold").toHaveAttribute("aria-expanded", "false");
        await click("button.ic-fs-fold");
        await animationFrame();
        expect("ol.ic-fs-steps").toHaveCount(1);
        expect("button.ic-fs-fold-link").toHaveAttribute("aria-expanded", "true");
    });

    test("one optional step left is said in the singular", async () => {
        await mountCard(stateOf({ collapsed: true, done: 3, optional_left: 1 }));
        expect("button.ic-fs-fold [aria-live]").toHaveText("First steps · 3 of 3 done · 1 optional left");
    });

    test("nothing is drawn when it is off or does not show", async () => {
        await mountCard({ enabled: false });
        expect("section.ic-fs").toHaveCount(0);
        firstStepsStore.data = stateOf({ visible: false });
        await animationFrame();
        expect("section.ic-fs").toHaveCount(0);
    });

    test("a layer's header is drawn over the steps with the state's header", async () => {
        class Header extends Component {
            static template = xml`<div class="test-fs-header" t-esc="props.header.profile"/>`;
            static props = { header: Object };
        }
        registerFirstStepsHeader(Header);
        await mountCard(stateOf({ header: { profile: "need_server" } }));
        expect(".test-fs-header").toHaveText("need_server");
    });
});

describe("FirstSteps — the shared state", () => {
    test("a failed /cloud/first_steps leaves the card out", async () => {
        patchWithCleanup(rpc, {
            _rpc() {
                return Promise.reject(new Error("down"));
            },
        });
        firstStepsStore.data = stateOf();
        const data = await loadFirstSteps();
        expect(data).toEqual({ enabled: false });
        expect(firstStepsStore.loaded).toBe(true);
        const dashboard = Object.create(ProjectDashboard.prototype);
        dashboard.firstSteps = firstStepsStore;
        expect(dashboard.firstStepsShown).toBe(false);
        expect(dashboard.firstStepsReopen).toBe(false);
    });

    test("the way back: on, something left and not shown", () => {
        expect(canReopenFirstSteps(stateOf({ visible: false, hidden: true }))).toBe(true);
        expect(canReopenFirstSteps(stateOf())).toBe(false);
        expect(canReopenFirstSteps(stateOf({ visible: false, pending: false }))).toBe(false);
        expect(canReopenFirstSteps({ enabled: false })).toBe(false);
    });

    test("the dashboard draws the card and its way back", () => {
        expect(ProjectDashboard.components.FirstSteps).toBe(FirstSteps);
        const page = getTemplate("incubacloud.ProjectDashboard");
        expect(page.querySelector("FirstSteps")).not.toBe(null);
        expect(page.querySelector("button.ic-fs-reopen").textContent).toBe("First steps");
    });
});

describe("FirstSteps — an instance's tab in the address", () => {
    test("?tab= is built and read back", () => {
        const url = _buildUrl("instance_detail", { project_id: 1, instance_id: 2, tab: "networking" });
        expect(url).toBe("/cloud/projects/1/instances/2?tab=networking");
        expect(_buildUrl("instance_detail", { project_id: 1, instance_id: 2 })).toBe(
            "/cloud/projects/1/instances/2"
        );
    });

    test("without ?tab= the route has no tab", () => {
        expect(_parseRoute("/cloud/projects/1/instances/2").params).toEqual({
            project_id: 1, instance_id: 2,
        });
    });
});
