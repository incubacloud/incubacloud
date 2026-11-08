import { describe, expect, test } from "@odoo/hoot";
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
