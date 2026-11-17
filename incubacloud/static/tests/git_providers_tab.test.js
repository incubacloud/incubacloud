import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, edit, queryAll, queryOne, waitFor } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { allowTranslations, mountWithCleanup, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { getTemplate } from "@web/core/templates";
import {
    blankCredential,
    CREDENTIAL_KINDS,
    deliveryArrived,
    GitProvidersTab,
    VERIFY_WAIT_MS,
} from "@incubacloud/components/git_providers_tab/git_providers_tab";
import { GitProvidersBlock } from "@incubacloud/components/settings/core_settings_block";
import { blocksOf } from "@incubacloud/components/settings/settings_registry";
import { relayIcon } from "@incubacloud/utils/icons";

/**
 * Settings → Git providers: the section saves each
 * provider on its own, so it never joins the page's save bar, and a new
 * credential starts as a project token with no token stored.
 */

describe("GitProvidersTab — class structure", () => {
    test("has its template and no props", () => {
        expect(GitProvidersTab.template).toBe("incubacloud.GitProvidersTab");
        expect(Object.keys(GitProvidersTab.props)).toEqual([]);
    });

    test("is the section Git providers of Settings, saved by itself", () => {
        const [block] = blocksOf("git-providers", { features: {} });
        expect(block.Component).toBe(GitProvidersBlock);
        expect(GitProvidersBlock.components.GitProvidersTab).toBe(GitProvidersTab);
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

describe("GitProvidersTab — a stored token", () => {
    beforeEach(() => allowTranslations());

    /** @returns {object} a stand-in of the tab, for its helpers */
    const helpers = () => Object.create(GitProvidersTab.prototype);

    test("shows as Stored until it is replaced or typed over", () => {
        const t = helpers();
        expect(t.tokenStored({ has_token: true, replacing: false, token: "" })).toBe(true);
        expect(t.tokenStored({ has_token: true, replacing: true, token: "" })).toBe(false);
        expect(t.tokenStored({ has_token: true, replacing: false, token: "glpat-x" })).toBe(false);
        expect(t.tokenStored(blankCredential())).toBe(false);
    });

    test("the input says New value over a stored token, the token's shape otherwise", () => {
        const t = helpers();
        expect(String(t.tokenPlaceholder({ has_token: true }))).toBe("New value");
        expect(t.tokenPlaceholder(blankCredential())).toBe("glpat-… / gldt-…");
    });

    test("every field of a credential row has its id, for its label", () => {
        const row = getTemplate("incubacloud.GitProviderEditor").querySelector(".ic-gitp-credrow");
        const fors = [...row.querySelectorAll("label")].map((l) => l.getAttribute("t-att-for"));
        expect(fors).toEqual([
            "credId(cred_index, 'namespace')",
            "credId(cred_index, 'kind')",
            "credId(cred_index, 'username')",
            "tokenStored(cred) ? undefined : credId(cred_index, 'token')",
            "credId(cred_index, 'expires')",
        ]);
        expect(helpers().credId(2, "token")).toBe("ic-gitp-cred-2-token");
    });

    test("Replace, a new token typed and saved; Keep stored sends none", async () => {
        const saved = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                if (url === "/cloud/get_git_providers") {
                    return Promise.resolve({
                        can_manage: true, network: "any",
                        providers: [{
                            id: 7, name: "GitLab.com", base_url: "https://gitlab.com",
                            ca_bundle: "",
                            credentials: [
                                { id: 1, namespace: "acme", kind: "group", username: "",
                                  has_token: true, expires_on: false, scopes: "api" },
                                { id: 2, namespace: "acme/web", kind: "project", username: "",
                                  has_token: true, expires_on: false, scopes: "api" },
                            ],
                        }],
                    });
                }
                if (url === "/cloud/get_git_hooks") {
                    return Promise.resolve({ hooks: [] });
                }
                if (url === "/cloud/save_git_provider") {
                    saved.push(params.vals);
                    return Promise.resolve({ ok: true, provider: { id: 7 }, warnings: [] });
                }
                return Promise.resolve({});
            },
        });
        await mountWithCleanup(GitProvidersTab, { env: { icon: relayIcon }, noMainContainer: true });
        await click(".ic-gitp-actions button:contains(Edit)");
        await waitFor(".ic-gitp-editor");
        // Both tokens are stored: «Stored» and Replace, no input, and the
        // label describes the button instead of naming it.
        expect(".ic-gitp-credrow .ic-set-secret").toHaveCount(2);
        expect(".ic-gitp-credrow input[type=password]").toHaveCount(0);
        expect(queryOne("#ic-gitp-cred-0-token-label").getAttribute("for")).toBe(null);
        expect("#ic-gitp-cred-0-token-replace").toHaveAttribute(
            "aria-describedby", "ic-gitp-cred-0-token-label"
        );
        expect("label[for=ic-gitp-cred-0-namespace]").toHaveText("Namespace");
        expect("input#ic-gitp-cred-0-namespace").toHaveValue("acme");

        // The first: Replace opens its input, focused and named by the label.
        await click("#ic-gitp-cred-0-token-replace");
        await animationFrame();
        expect("input#ic-gitp-cred-0-token").toBeFocused();
        expect("input#ic-gitp-cred-0-token").toHaveAttribute("placeholder", "New value");
        expect("#ic-gitp-cred-0-token-label").toHaveAttribute("for", "ic-gitp-cred-0-token");
        await edit("glpat-new");
        // The second: Replace, something typed, then Keep stored.
        await click("#ic-gitp-cred-1-token-replace");
        await animationFrame();
        await edit("typed-by-mistake");
        await click(queryAll(".ic-gitp-credrow button:contains(Keep stored)")[1]);
        await animationFrame();
        expect("#ic-gitp-cred-1-token-replace").toBeFocused();
        expect("input#ic-gitp-cred-1-token").toHaveCount(0);

        await click(".ic-gitp-actions button:contains(Save)");
        await animationFrame();
        expect(saved.length).toBe(1);
        expect(saved[0].credentials.map((c) => [c.id, c.token])).toEqual([
            [1, "glpat-new"], [2, ""],
        ]);
        expect("replacing" in saved[0].credentials[0]).toBe(false);
    });
});

describe("GitProvidersTab — Test connection", () => {
    const NOTICE = "GitLab does not let the panel read this token's permissions "
        + "(fine-grained tokens do not allow it). To deploy, it is enough that git works.";

    /**
     * Mount the tab with one provider whose test answers *answer*, and run
     * «Test connection».
     *
     * @param {Object} answer what ``/cloud/test_git_provider`` answers
     * @returns {Promise<void>}
     */
    async function testWith(answer) {
        patchWithCleanup(rpc, {
            _rpc(url) {
                if (url === "/cloud/get_git_providers") {
                    return Promise.resolve({
                        can_manage: true, network: "any",
                        providers: [{
                            id: 7, name: "GitLab.com", base_url: "https://gitlab.com",
                            ca_bundle: "", last_check_at: false, credentials: [],
                        }],
                    });
                }
                if (url === "/cloud/get_git_hooks") {
                    return Promise.resolve({ hooks: [] });
                }
                if (url === "/cloud/test_git_provider") {
                    return Promise.resolve({
                        ok: true, provider: { id: 7, name: "GitLab.com", credentials: [] },
                        ...answer,
                    });
                }
                return Promise.resolve({});
            },
        });
        await mountWithCleanup(GitProvidersTab, { env: { icon: relayIcon }, noMainContainer: true });
        await waitFor(".ic-gitp-test input");
        await click(".ic-gitp-test input");
        await edit("https://gitlab.com/acme/addons");
        await click(".ic-gitp-test button");
        await animationFrame();
    }

    test("git working and the token unreadable: the notice is informational, the git line is not an error", async () => {
        await testWith({
            git: { code: "ok", message: "It works." },
            api: { code: "forbidden", message: "The token lacks the scope or role this needs." },
            notice: { code: "token_unreadable", message: NOTICE },
        });
        expect(".rl-set-inline-msg--error").toHaveCount(0);
        expect(".ic-gitp-notice").toHaveCount(1);
        expect(".ic-gitp-notice").toHaveClass("rl-set-inline-msg--info");
        expect(".ic-gitp-notice").toHaveText(NOTICE);
        expect(".rl-set-inline-msg--success").toHaveCount(1);
    });

    test("git failing keeps the error and shows no notice", async () => {
        await testWith({
            git: { code: "auth", message: "The server refused the credential." },
            api: { code: "forbidden", message: "The token lacks the scope or role this needs." },
            notice: null,
        });
        expect(".ic-gitp-notice").toHaveCount(0);
        expect(".rl-set-inline-msg--error").toHaveCount(1);
        expect(".rl-set-inline-msg--error").toHaveText("git: The server refused the credential.");
    });

    test("git and the token both fine shows neither error nor notice", async () => {
        await testWith({
            git: { code: "ok", message: "It works." },
            api: { code: "ok", message: "It works." },
            notice: null,
        });
        expect(".ic-gitp-notice").toHaveCount(0);
        expect(".rl-set-inline-msg--error").toHaveCount(0);
        expect(".rl-set-inline-msg--success").toHaveCount(2);
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
        hooks: [], hooksError: null, verifying: null, hookResult: {},
        hookSecret: {}, hookBusy: null,
    };
    // The waits of «Verify» pass at once.
    comp.safePoll = { alive: true, schedule: (fn) => fn() };
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
        expect(urls).toEqual([
            "/cloud/delete_git_provider", "/cloud/get_git_providers", "/cloud/get_git_hooks",
        ]);
        expect(comp.state.providers).toEqual([]);
    });
});

/** A webhook of acme/web as ``get_git_hooks`` lists it. */
function hook(vals = {}) {
    return {
        id: 3, path: "acme/web", scope: "project", state: "pending", by_api: true,
        last_delivery_at: false, url: "https://panel.example.com/cloud/gitlab/webhook/abc",
        failed_events: [], ...vals,
    };
}

describe("GitProvidersTab — webhooks", () => {
    beforeEach(() => allowTranslations());

    test("a delivery counts when it is not older than the request", () => {
        const at = "2026-11-08 10:00:05";
        expect(deliveryArrived(hook({ last_delivery_at: "2026-11-08 10:00:05" }), at)).toBe(true);
        expect(deliveryArrived(hook({ last_delivery_at: "2026-11-08 10:00:09" }), at)).toBe(true);
        expect(deliveryArrived(hook({ last_delivery_at: "2026-11-08 09:59:59" }), at)).toBe(false);
        expect(deliveryArrived(hook(), at)).toBe(false);
        expect(deliveryArrived(undefined, at)).toBe(false);
    });

    test("Verify polls the webhooks until the test delivery shows", async () => {
        const urls = [];
        let reads = 0;
        patchWithCleanup(rpc, {
            _rpc(url) {
                urls.push(url);
                if (url === "/cloud/verify_git_hook") {
                    return Promise.resolve({ ok: true, verify_requested_at: "2026-11-08 10:00:05" });
                }
                reads += 1;
                const last = reads < 2 ? false : "2026-11-08 10:00:06";
                return Promise.resolve({ hooks: [hook({ last_delivery_at: last, state: "active" })] });
            },
        });
        const comp = tab();
        await comp.verifyHook(hook());
        expect(urls).toEqual(["/cloud/verify_git_hook", "/cloud/get_git_hooks", "/cloud/get_git_hooks"]);
        expect(comp.state.hookResult[3].success).toMatch(/arrived/);
        expect(comp.state.hooks[0].state).toBe("active");
        expect(comp.state.verifying).toBe(null);
    });

    test("Verify says where GitLab must reach when nothing arrives in time", async () => {
        let now = 0;
        patchWithCleanup(Date, { now: () => now });
        patchWithCleanup(rpc, {
            _rpc(url) {
                if (url === "/cloud/verify_git_hook") {
                    return Promise.resolve({ ok: true, verify_requested_at: "2026-11-08 10:00:05" });
                }
                now += VERIFY_WAIT_MS / 2;
                return Promise.resolve({ hooks: [hook()] });
            },
        });
        const comp = tab();
        await comp.verifyHook(hook());
        expect(comp.state.hookResult[3].error).toMatch(/cloud\/gitlab\/webhook\/abc/);
    });

    test("a refused Verify shows the route's reason without polling", async () => {
        const urls = [];
        patchWithCleanup(rpc, {
            _rpc(url) {
                urls.push(url);
                return Promise.resolve({ ok: false, error: "wait a few seconds" });
            },
        });
        const comp = tab();
        await comp.verifyHook(hook());
        expect(urls).toEqual(["/cloud/verify_git_hook"]);
        expect(comp.state.hookResult[3].error).toBe("wait a few seconds");
    });

    test("Rotate asks first, then shows the new secret", async () => {
        const calls = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                calls.push([url, params]);
                return Promise.resolve({ ok: true, url: "https://p/x", secret: "s3cr3t" });
            },
        });
        const comp = tab();
        const done = comp.revealHookSecret(hook(), true);
        expect(comp.state.confirmDialog.isDanger).toBe(true);
        comp.state.confirmDialog.onConfirm();
        await done;
        expect(calls).toEqual([["/cloud/reveal_git_hook_secret", { hook_id: 3, rotate: true }]]);
        expect(comp.state.hookSecret[3]).toEqual({ url: "https://p/x", secret: "s3cr3t" });
        comp.hideHookSecret(hook());
        expect(comp.state.hookSecret).toEqual({});
    });

    test("Reprocess reports a delivery that fails again", async () => {
        patchWithCleanup(rpc, {
            _rpc(url) {
                return Promise.resolve(url === "/cloud/reprocess_git_event"
                    ? { ok: true, processed: false, error: "RuntimeError: boom" }
                    : { hooks: [] });
            },
        });
        const comp = tab();
        await comp.reprocessEvent(hook(), { id: 40 });
        expect(comp.state.hookResult[3].error).toMatch(/RuntimeError: boom/);
        expect(comp.state.hookBusy).toBe(null);
    });

    test("a deleted webhook leaves the list", async () => {
        patchWithCleanup(rpc, {
            _rpc(url) {
                return Promise.resolve(url === "/cloud/delete_git_hook" ? { ok: true } : { hooks: [] });
            },
        });
        const comp = tab();
        comp.state.hooks = [hook()];
        const done = comp.deleteHook(hook());
        comp.state.confirmDialog.onConfirm();
        await done;
        expect(comp.state.hooks).toEqual([]);
        expect(comp.state.hooksNotice).toBe(null);
    });

    test("a webhook deleted in the panel only says that GitLab keeps it", async () => {
        const warning = "The panel cannot delete the webhook in GitLab";
        patchWithCleanup(rpc, {
            _rpc(url) {
                return Promise.resolve(
                    url === "/cloud/delete_git_hook" ? { ok: true, warning } : { hooks: [] },
                );
            },
        });
        const comp = tab();
        comp.state.hooks = [hook()];
        const done = comp.deleteHook(hook());
        comp.state.confirmDialog.onConfirm();
        await done;
        expect(comp.state.hooks).toEqual([]);
        expect(comp.state.hooksNotice).toBe(warning);
    });
});

/**
 * The webhooks' list in the tab's template: the notice of a webhook
 * deleted in the panel only, the load error and the empty list are three
 * messages, and none hides another. The notice once sat between the
 * error's ``t-if`` and the empty list's ``t-elif``, which then hung on the
 * notice: a notice hid «No webhooks yet.», and a load error showed it.
 *
 * Read from the template rather than mounted (see ic_confirm_dialog.test.js).
 */
describe("GitProvidersTab — webhooks list", () => {
    /**
     * The message of the list whose condition is *condition*.
     *
     * @param {string} attribute ``t-if`` or ``t-elif``
     * @param {string} condition its expression
     * @returns {Element} the element
     */
    function message(attribute, condition) {
        return getTemplate("incubacloud.GitProvidersTab").querySelector(
            `[${attribute}="${condition}"]`,
        );
    }

    test("the empty list hangs on the load error, not on the notice", () => {
        const empty = message("t-elif", "!state.hooks.length");
        expect(empty.previousElementSibling.getAttribute("t-if")).toBe("state.hooksError");
    });

    test("the notice stands on its own, before the error and the list", () => {
        const notice = message("t-if", "state.hooksNotice");
        expect(notice.nextElementSibling.getAttribute("t-if")).toBe("state.hooksError");
        expect(notice.hasAttribute("t-elif")).toBe(false);
    });
});
