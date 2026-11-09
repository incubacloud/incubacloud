import { describe, expect, test } from "@odoo/hoot";
import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";

/**
 * "Sync with project" modal: the select-all boxes.
 *
 * Each section (pip, apt, repos) has a master checkbox over its
 * transfer rows (instance only / project only), and one over all of
 * them. The conflicts are not in it: each is a choice of a winner, and
 * ticking them all would decide every one for the user. The modal's
 * "Apply selected" button is enabled by ``syncHasSelections``, so the
 * tests read it too.
 */

const DIFF = {
    pip: {
        only_instance: ["requests==2.31", "lxml"],
        only_project: ["pandas"],
        conflicts: [{ name: "numpy", instance_line: "numpy==1", project_line: "numpy==2" }],
    },
    apt: { only_instance: ["git"], only_project: [], conflicts: [] },
    repos: {
        only_instance: [{ url: "https://github.com/a/b", branch: "19.0" }],
        only_project: [{ url: "https://github.com/c/d", branch: "19.0" }],
        conflicts: [],
    },
};

/**
 * A modal over *diff* with nothing ticked, on the real component's
 * methods and getters, without mounting it.
 */
function modal(diff = DIFF) {
    const ctx = Object.create(InstanceDetail.prototype);
    ctx.state = { syncModal: { diff, actions: null } };
    ctx.state.syncModal.actions = ctx._buildSyncActions(diff);
    return ctx;
}

describe("Sync modal: select all in a section", () => {
    test("starts with nothing ticked and Apply disabled", () => {
        const m = modal();
        expect(m.syncSectionState("pip")).toEqual({
            total: 3,
            selected: 0,
            checked: false,
            indeterminate: false,
        });
        expect(m.syncHasSelections).toBe(false);
    });

    test("ticking the master ticks every row of the section, both directions", () => {
        const m = modal();
        m.toggleSyncSection("pip");
        const a = m.state.syncModal.actions.pip;
        expect(a.push).toEqual({ "requests==2.31": true, lxml: true });
        expect(a.pull).toEqual({ pandas: true });
        expect(m.syncSectionState("pip").checked).toBe(true);
        expect(m.syncSectionState("pip").indeterminate).toBe(false);
    });

    test("it leaves the other sections and the conflicts alone", () => {
        const m = modal();
        m.toggleSyncSection("pip");
        const actions = m.state.syncModal.actions;
        expect(actions.pip.conflicts).toEqual({ numpy: null });
        expect(m.syncSectionState("apt").selected).toBe(0);
        expect(m.syncSectionState("repos").selected).toBe(0);
    });

    test("Apply selected is enabled once the master is ticked", () => {
        const m = modal();
        m.toggleSyncSection("apt");
        expect(m.syncHasSelections).toBe(true);
    });

    test("ticking the master again clears the section", () => {
        const m = modal();
        m.toggleSyncSection("pip");
        m.toggleSyncSection("pip");
        expect(m.syncSectionState("pip").selected).toBe(0);
        expect(m.syncHasSelections).toBe(false);
    });

    test("part of a section ticked is indeterminate, not checked", () => {
        const m = modal();
        m.toggleSyncAction("pip", "push", "lxml");
        expect(m.syncSectionState("pip")).toEqual({
            total: 3,
            selected: 1,
            checked: false,
            indeterminate: true,
        });
    });

    test("the master on a partly ticked section ticks the rest", () => {
        const m = modal();
        m.toggleSyncAction("pip", "pull", "pandas");
        m.toggleSyncSection("pip");
        expect(m.syncSectionState("pip").checked).toBe(true);
    });

    test("unticking one row of a full section makes it indeterminate", () => {
        const m = modal();
        m.toggleSyncSection("pip");
        m.toggleSyncAction("pip", "push", "lxml");
        expect(m.syncSectionState("pip").indeterminate).toBe(true);
    });

    test("a section with only conflicts has nothing to select", () => {
        const m = modal({
            pip: { only_instance: [], only_project: [], conflicts: DIFF.pip.conflicts },
            apt: { only_instance: [], only_project: [], conflicts: [] },
            repos: { only_instance: [], only_project: [], conflicts: [] },
        });
        expect(m.syncSectionState("pip").total).toBe(0);
        m.toggleSyncSection("pip");
        expect(m.syncSectionState("pip").checked).toBe(false);
        expect(m.syncHasSelections).toBe(false);
    });
});

describe("Sync modal: select all changes", () => {
    test("ticks and clears every section at once", () => {
        const m = modal();
        m.toggleSyncAll();
        for (const sec of ["pip", "apt", "repos"]) {
            expect(m.syncSectionState(sec).checked).toBe(true);
        }
        expect(m.syncAllState).toEqual({
            total: 6,
            selected: 6,
            checked: true,
            indeterminate: false,
        });
        expect(m.syncHasSelections).toBe(true);
        m.toggleSyncAll();
        expect(m.syncAllState.selected).toBe(0);
        expect(m.syncHasSelections).toBe(false);
    });

    test("one section ticked makes it indeterminate", () => {
        const m = modal();
        m.toggleSyncSection("repos");
        expect(m.syncAllState).toEqual({
            total: 6,
            selected: 2,
            checked: false,
            indeterminate: true,
        });
    });
});
