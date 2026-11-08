import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import {
    blankCredential,
    CREDENTIAL_KINDS,
    GitProvidersTab,
} from "@incubacloud/components/git_providers_tab/git_providers_tab";
import { OWN_SETTINGS_TABS, Settings } from "@incubacloud/components/settings/settings";

/**
 * Settings → Git providers (plan-gitlab.md §4.11-1): the tab saves each
 * provider on its own, so it is not one of Settings' own tabs, and a new
 * credential starts as a project token with no token stored.
 */

describe("GitProvidersTab — class structure", () => {
    test("has its template and no props", () => {
        expect(GitProvidersTab.template).toBe("incubacloud.GitProvidersTab");
        expect(Object.keys(GitProvidersTab.props)).toEqual([]);
    });

    test("is a component of Settings, saved by itself", () => {
        expect(Settings.components.GitProvidersTab).toBe(GitProvidersTab);
        expect(OWN_SETTINGS_TABS.includes("git_providers")).toBe(false);
    });
});

describe("GitProvidersTab — credentials", () => {
    test("offers the four kinds of GitLab token", () => {
        expect([...CREDENTIAL_KINDS]).toEqual(["personal", "group", "project", "deploy"]);
    });

    test("a blank credential is a new project token, nothing stored", () => {
        const cred = blankCredential();
        expect(cred.id).toBe(false);
        expect(cred.kind).toBe("project");
        expect(cred.token).toBe("");
        expect(cred.has_token).toBe(false);
        expect(blankCredential()).not.toBe(cred);
    });
});

/**
 * A tab stand-in listing one provider, as the panel would.
 *
 * No mounting (see ic_confirm_dialog.test.js): ``deleteProvider`` is the
 * shipped one, on a stand-in holding the state it reads.
 *
 * @returns {object} the stand-in
 */
function tab() {
    const comp = Object.create(GitProvidersTab.prototype);
    comp.state = {
        loading: false, loadError: null, network: "any", canManage: true,
        providers: [{ id: 7, name: "GitLab.com" }], draft: null,
        testResult: {}, deleteError: {}, confirmDialog: null,
    };
    return comp;
}

/**
 * Delete provider 7 on *comp*, confirming the dialog.
 *
 * @param {object} comp the stand-in
 * @returns {Promise<void>} when the delete is over
 */
async function confirmDelete(comp) {
    const done = comp.deleteProvider(comp.state.providers[0]);
    comp.state.confirmDialog.onConfirm();
    await done;
}

describe("GitProvidersTab — delete", () => {
    beforeEach(() => allowTranslations());

    test("a refused delete says why under its provider, not in the test's result", async () => {
        patchWithCleanup(rpc, {
            _rpc: () => Promise.resolve({ ok: false, error: "1 repository lines use GitLab.com" }),
        });
        const comp = tab();
        await confirmDelete(comp);
        expect(comp.state.deleteError[7]).toBe("1 repository lines use GitLab.com");
        expect(comp.state.testResult).toEqual({});
    });

    test("a server that does not answer is said, never thrown", async () => {
        patchWithCleanup(rpc, { _rpc: () => Promise.reject(new Error("offline")) });
        const comp = tab();
        await confirmDelete(comp);
        expect(String(comp.state.deleteError[7])).toBe("Could not reach the server.");
    });

    test("a delete that goes through clears the last error and reloads", async () => {
        const urls = [];
        patchWithCleanup(rpc, {
            _rpc(url) {
                urls.push(url);
                return Promise.resolve(
                    url === "/cloud/get_git_providers" ? { providers: [] } : { ok: true },
                );
            },
        });
        const comp = tab();
        comp.state.deleteError[7] = "earlier";
        await confirmDelete(comp);
        expect(comp.state.deleteError).toEqual({});
        expect(urls).toEqual(["/cloud/delete_git_provider", "/cloud/get_git_providers"]);
        expect(comp.state.providers).toEqual([]);
    });
});
