import { describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { _refreshesNavCounts } from "@incubacloud/app/app";
import { HostDetail } from "@incubacloud/components/host_detail/host_detail";
import { ProjectDetail } from "@incubacloud/components/project_detail/project_detail";
import { BackupBackendDetail } from "@incubacloud/components/backup_backend_detail/backup_backend_detail";

/**
 * The sidebar's count badges are asked again (``/cloud/get_nav_counts``)
 * when the panel moves between the sidebar's sections, lands on one of
 * them, and after a project, a host or a backup destination is created
 * or deleted — not only when the page loads.
 *
 * No mounting (see ic_confirm_dialog.test.js): the shipped methods run
 * on a stand-in whose ``env`` records what they ask for, and the
 * server is ``rpc`` answering what each test says.
 */

/**
 * Have the server answer *answers* through ``rpc`` for this test.
 *
 * @param {object} answers by route: the answer
 */
function answering(answers) {
    patchWithCleanup(rpc, {
        _rpc(url) {
            return Promise.resolve(answers[url]);
        },
    });
}

/**
 * A stand-in of *Component* whose env records navigations and badge
 * refreshes, and whose confirmation says yes.
 *
 * @param {Function} Component the component class
 * @param {object} fields own state and props of the stand-in
 * @returns {object} the stand-in, with ``calls``: ``navigate`` and
 *     ``counts`` (how many refreshes were asked)
 */
function standIn(Component, fields) {
    const comp = Object.create(Component.prototype);
    const calls = { navigate: [], counts: 0, errors: [] };
    Object.assign(comp, fields, {
        calls,
        env: {
            toast: { success() {}, info() {}, warning() {}, error: (m) => calls.errors.push(m) },
            navigate: (route, params) => calls.navigate.push([route, params]),
            refreshNavCounts: () => { calls.counts += 1; },
        },
        _confirm: () => Promise.resolve(true),
        _snapshotForm() {},
        validator: { validate: () => ({ isValid: true }) },
    });
    return comp;
}

describe("nav counts — when a move asks them again", () => {
    test("changing section asks", () => {
        expect(_refreshesNavCounts("/cloud/projects/4", "/cloud/hosts/2")).toBe(true);
        expect(_refreshesNavCounts("/cloud/hosts", "/cloud/settings?tab=general")).toBe(true);
        expect(_refreshesNavCounts("/cloud/settings", "/cloud/backup-backends/3")).toBe(true);
    });

    test("landing on a section's own page asks, as a sidebar click does", () => {
        expect(_refreshesNavCounts("/cloud/hosts/2", "/cloud/hosts")).toBe(true);
        expect(_refreshesNavCounts("/cloud/projects/4/instances/5", "/cloud/projects")).toBe(true);
        expect(_refreshesNavCounts("/cloud/hosts", "/cloud/hosts/")).toBe(true);
    });

    test("moving inside a section does not", () => {
        expect(_refreshesNavCounts("/cloud/projects/4", "/cloud/projects/4/instances/5")).toBe(false);
        expect(_refreshesNavCounts("/cloud/hosts", "/cloud/hosts/2")).toBe(false);
        expect(_refreshesNavCounts("/cloud/hosts/new", "/cloud/hosts/7")).toBe(false);
    });
});

describe("nav counts — a create or a delete asks them again", () => {
    test("a project created", async () => {
        allowTranslations();
        answering({ "/cloud/create_project": { id: 9 } });
        const comp = standIn(ProjectDetail, {
            props: { project_id: false },
            state: {
                form: { name: "Shop", repos: [] }, repos: [], pipDeps: "", aptDeps: "",
                members: [], selectedTags: [], saving: false,
            },
        });
        await comp.save();
        expect(comp.calls.errors).toEqual([]);
        expect(comp.calls.counts).toBe(1);
        expect(comp.calls.navigate).toEqual([["project_detail", { project_id: 9 }]]);
    });

    test("a project deleted", async () => {
        allowTranslations();
        answering({ "/cloud/delete_project": { ok: true } });
        const comp = standIn(ProjectDetail, {
            props: { project_id: 9 }, state: { form: { name: "Shop" } },
        });
        await comp.deleteProject();
        expect(comp.calls.counts).toBe(1);
        expect(comp.calls.navigate).toEqual([["projects", undefined]]);
    });

    test("a project that could not be deleted asks nothing", async () => {
        allowTranslations();
        answering({ "/cloud/delete_project": { error: "It has instances." } });
        const comp = standIn(ProjectDetail, {
            props: { project_id: 9 }, state: { form: { name: "Shop" } },
        });
        await comp.deleteProject();
        expect(comp.calls.counts).toBe(0);
    });

    test("a host deleted", async () => {
        allowTranslations();
        answering({ "/cloud/delete_host": { ok: true, job_id: false } });
        const comp = standIn(HostDetail, {
            props: { host_id: 2 },
            state: { host: { name: "h1", traefik_deployed: false }, deleting: false },
        });
        await comp.deleteHost();
        expect(comp.calls.counts).toBe(1);
        expect(comp.calls.navigate).toEqual([["hosts", undefined]]);
    });

    test("a backup destination deleted", async () => {
        allowTranslations();
        answering({ "/cloud/delete_backup_backend": { ok: true } });
        const comp = standIn(BackupBackendDetail, {
            props: { backend_id: 3 }, state: { deleting: false },
        });
        await comp.deleteBackend();
        expect(comp.calls.counts).toBe(1);
        expect(comp.calls.navigate).toEqual([["backup_backends", undefined]]);
    });

    test("a backup destination created", async () => {
        allowTranslations();
        answering({ "/cloud/create_backup_backend": { id: 5 } });
        const comp = standIn(BackupBackendDetail, {
            props: { backend_id: false }, state: { form: { name: "R2" }, saving: false },
            isNew: true,
        });
        await comp.save();
        expect(comp.calls.errors).toEqual([]);
        expect(comp.calls.counts).toBe(1);
        expect(comp.calls.navigate).toEqual([["backup_backend_detail", { backend_id: 5 }]]);
    });
});
