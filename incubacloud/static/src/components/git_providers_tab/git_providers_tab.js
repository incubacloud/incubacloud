/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { RlSelect } from "../rl_select/rl_select";
import { confirmVia } from "../../utils/use_confirm";
import { useSafePoll } from "../../utils/use_safe_poll";
import { parseUTC, userLocale } from "../../utils/dates";

/** The token kinds GitLab issues, as the panel names them. */
export const CREDENTIAL_KINDS = Object.freeze([
    "personal", "group", "project", "deploy",
]);

/**
 * A blank credential row of the editor.
 *
 * @returns {Object} the row, without id (a new credential)
 */
export function blankCredential() {
    return {
        id: false, namespace: "", kind: "project", username: "", token: "",
        expires_on: "", has_token: false, scopes: "", token_user: "",
    };
}

/** How often «Verify» reads the webhooks again while it waits, in ms. */
export const VERIFY_POLL_MS = 1500;

/**
 * How long «Verify» waits for GitLab's test delivery, in ms: GitLab
 * sends it at once, and the route does not wait.
 */
export const VERIFY_WAIT_MS = 15000;

/**
 * Whether *hook* got a delivery since «Verify» was asked at *requestedAt*.
 *
 * Both are the panel's UTC strings ("YYYY-MM-DD HH:MM:SS"), which sort as
 * they read; a delivery in the same second counts.
 *
 * @param {Object|undefined} hook a webhook as ``get_git_hooks`` lists it
 * @param {string} requestedAt the ``verify_requested_at`` the route returned
 * @returns {boolean}
 */
export function deliveryArrived(hook, requestedAt) {
    return !!(hook && hook.last_delivery_at && requestedAt
        && hook.last_delivery_at >= requestedAt);
}

/**
 * Settings → Git providers: the GitLab servers the panel reads, deploys
 * and rebuilds from, and their credentials.
 *
 * Each provider is saved on its own, with its credentials, by its own
 * Save: the tab joins neither Settings' footer nor its unsaved-changes
 * guard. A token is write-only: the editor never receives it, and an
 * empty token field keeps the stored one.
 */
export class GitProvidersTab extends Component {
    static template = "incubacloud.GitProvidersTab";
    static components = { IcConfirmDialog, RlSelect };
    static props = {};

    setup() {
        this.state = useState({
            loading: true,
            loadError: null,
            network: "any",
            canManage: false,
            providers: [],
            // The provider being edited (a copy), or null.
            draft: null,
            saving: false,
            saveError: null,
            // Warnings of the last save, per provider id.
            warnings: {},
            // «Test connection»: the URL typed and the last answer, per id.
            testUrl: {},
            testing: null,
            testResult: {},
            // Why the last delete of a provider failed, per id.
            deleteError: {},
            confirmDialog: null,
            // GitLab webhooks of the panel, and what was last done
            // to each, per id.
            hooks: [],
            hooksError: null,
            // What the last delete of a webhook left in GitLab, or null.
            hooksNotice: null,
            verifying: null,
            hookResult: {},
            // The URL and secret shown for a webhook, per id.
            hookSecret: {},
            hookBusy: null,
        });
        this.safePoll = useSafePoll();
        onWillStart(() => this.load());
    }

    /**
     * The label of a webhook's state.
     *
     * @param {string} state ``pending``, ``active``, ``failing`` or ``orphan``
     * @returns {string}
     */
    hookStateLabel(state) {
        return {
            pending: _t("No delivery yet"),
            active: _t("Active"),
            failing: _t("Disabled by GitLab"),
            orphan: _t("Unused"),
        }[state] || state;
    }

    /**
     * The class of a webhook's state pill (relay.scss).
     *
     * @param {string} state a webhook's state
     * @returns {string}
     */
    hookStatePill(state) {
        return {
            pending: "waiting", active: "active", failing: "error", orphan: "stopped",
        }[state] || "unknown";
    }

    /**
     * A date of the panel (UTC) as the reader writes dates.
     *
     * @param {string} value a UTC datetime string
     * @returns {string}
     */
    dateLabel(value) {
        const date = parseUTC(value);
        return date ? date.toLocaleString(userLocale()) : "";
    }

    /**
     * Load the webhooks of the panel; a failure only hides them.
     *
     * @returns {Promise<void>}
     */
    async loadHooks() {
        try {
            const data = await rpc("/cloud/get_git_hooks", {});
            this.state.hooks = data.hooks || [];
            this.state.hooksError = null;
        } catch {
            this.state.hooksError = _t("Failed to load the webhooks.");
        }
    }

    /**
     * Wait *ms* milliseconds, unless the tab is closed first.
     *
     * @param {number} ms the wait
     * @returns {Promise<void>} never settled once the tab is closed
     */
    _sleep(ms) {
        return new Promise((resolve) => this.safePoll.schedule(resolve, ms));
    }

    /**
     * «Verify»: ask GitLab for a test delivery, then read the webhooks
     * until the delivery shows or :data:`VERIFY_WAIT_MS` runs out.
     *
     * @param {Object} hook a webhook as ``get_git_hooks`` lists it
     * @returns {Promise<void>}
     */
    async verifyHook(hook) {
        this.state.verifying = hook.id;
        delete this.state.hookResult[hook.id];
        try {
            const res = await rpc("/cloud/verify_git_hook", { hook_id: hook.id });
            if (!res.ok) {
                this.state.hookResult[hook.id] = { error: res.error };
                return;
            }
            const deadline = Date.now() + VERIFY_WAIT_MS;
            while (Date.now() < deadline) {
                await this._sleep(VERIFY_POLL_MS);
                await this.loadHooks();
                const fresh = this.state.hooks.find((h) => h.id === hook.id);
                if (deliveryArrived(fresh, res.verify_requested_at)) {
                    this.state.hookResult[hook.id] = {
                        success: _t("GitLab's test delivery arrived: the webhook is active."),
                    };
                    return;
                }
            }
            this.state.hookResult[hook.id] = {
                error: _t(
                    "GitLab sent the test, but the panel received nothing: check that GitLab reaches %(url)s.",
                    { url: hook.url || _t("this panel's address") },
                ),
            };
        } catch {
            this.state.hookResult[hook.id] = { error: _t("Could not reach the server.") };
        } finally {
            this.state.verifying = null;
        }
    }

    /**
     * Show a webhook's URL and secret, after giving it a new secret when
     * *rotate* (asked first: GitLab gets it too when the panel set the
     * webhook up; a webhook set up by hand needs it pasted there).
     *
     * @param {Object} hook a webhook as ``get_git_hooks`` lists it
     * @param {boolean} rotate whether to give it a new secret
     * @returns {Promise<void>}
     */
    async revealHookSecret(hook, rotate = false) {
        if (rotate) {
            const ok = await confirmVia(this.state, {
                title: _t("Rotate webhook secret"),
                message: hook.by_api
                    ? _t("Give the webhook of %(path)s a new secret? The panel updates it in GitLab too.", { path: hook.path })
                    : _t("Give the webhook of %(path)s a new secret? Deliveries fail until you paste it in GitLab.", { path: hook.path }),
                confirmLabel: _t("Rotate"),
                isDanger: true,
            });
            if (!ok) {
                return;
            }
        }
        this.state.hookBusy = hook.id;
        delete this.state.hookResult[hook.id];
        try {
            const res = await rpc("/cloud/reveal_git_hook_secret", { hook_id: hook.id, rotate });
            if (!res.ok) {
                this.state.hookResult[hook.id] = { error: res.error };
                return;
            }
            this.state.hookSecret[hook.id] = { url: res.url, secret: res.secret };
            if (rotate) {
                this.state.hookResult[hook.id] = { success: _t("The webhook has a new secret.") };
            }
        } catch {
            this.state.hookResult[hook.id] = { error: _t("Could not reach the server.") };
        } finally {
            this.state.hookBusy = null;
        }
    }

    /**
     * Hide the URL and secret shown for *hook*.
     *
     * @param {Object} hook a webhook
     */
    hideHookSecret(hook) {
        delete this.state.hookSecret[hook.id];
    }

    /**
     * Delete a webhook in GitLab (when the panel set it up) and in the
     * panel, after asking.
     *
     * @param {Object} hook a webhook as ``get_git_hooks`` lists it
     * @returns {Promise<void>}
     */
    async deleteHook(hook) {
        const ok = await confirmVia(this.state, {
            title: _t("Delete webhook"),
            message: _t(
                "Delete the webhook of %(path)s? Pushes to it stop rebuilding until it is registered again.",
                { path: hook.path },
            ),
            confirmLabel: _t("Delete"),
            isDanger: true,
        });
        if (!ok) {
            return;
        }
        this.state.hookBusy = hook.id;
        this.state.hooksNotice = null;
        delete this.state.hookResult[hook.id];
        try {
            const res = await rpc("/cloud/delete_git_hook", { hook_id: hook.id });
            if (!res.ok) {
                this.state.hookResult[hook.id] = { error: res.error };
                return;
            }
            // The webhook leaves the list: what GitLab keeps of it is said above.
            this.state.hooksNotice = res.warning || null;
            delete this.state.hookSecret[hook.id];
            await this.loadHooks();
        } catch {
            this.state.hookResult[hook.id] = { error: _t("Could not reach the server.") };
        } finally {
            this.state.hookBusy = null;
        }
    }

    /**
     * Dispatch again a delivery of *hook* whose dispatch failed.
     *
     * @param {Object} hook the webhook the delivery came to
     * @param {Object} event a delivery of ``hook.failed_events``
     * @returns {Promise<void>}
     */
    async reprocessEvent(hook, event) {
        this.state.hookBusy = hook.id;
        delete this.state.hookResult[hook.id];
        try {
            const res = await rpc("/cloud/reprocess_git_event", { event_id: event.id });
            if (!res.ok) {
                this.state.hookResult[hook.id] = { error: res.error };
            } else if (res.processed) {
                this.state.hookResult[hook.id] = { success: _t("The delivery was processed.") };
            } else {
                this.state.hookResult[hook.id] = {
                    error: _t("The delivery failed again: %(error)s", { error: res.error }),
                };
            }
            await this.loadHooks();
        } catch {
            this.state.hookResult[hook.id] = { error: _t("Could not reach the server.") };
        } finally {
            this.state.hookBusy = null;
        }
    }

    /** The token kinds, as options of the kind selector. */
    get kindOptions() {
        const labels = {
            personal: _t("Personal access token"),
            group: _t("Group access token"),
            project: _t("Project access token"),
            deploy: _t("Deploy token"),
        };
        return CREDENTIAL_KINDS.map((value) => ({ value, label: labels[value] }));
    }

    /**
     * Load the providers from the panel.
     *
     * @returns {Promise<void>}
     */
    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const data = await rpc("/cloud/get_git_providers", {});
            this.state.network = data.network || "any";
            this.state.canManage = !!data.can_manage;
            this.state.providers = data.providers || [];
            await this.loadHooks();
        } catch {
            this.state.loadError = _t("Failed to load the Git providers.");
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * The text of a credential's scopes, or what its absence means.
     *
     * @param {Object} cred a credential as the panel lists it
     * @returns {string}
     */
    scopesLabel(cred) {
        if (cred.kind === "deploy") {
            return _t("no API");
        }
        if (!cred.scopes) {
            return _t("scopes not read");
        }
        return cred.scopes.split(" ").join(", ");
    }

    /**
     * When a credential's token expires, as the list says it.
     *
     * @param {Object} cred a credential with ``expires_on``
     * @returns {string}
     */
    expiresLabel(cred) {
        return _t("expires on %(date)s", { date: cred.expires_on });
    }

    /**
     * The placeholder of a credential's token field: a stored token is
     * kept when the field is left blank.
     *
     * @param {Object} cred a credential row of the editor
     * @returns {string}
     */
    tokenPlaceholder(cred) {
        return cred.has_token
            ? _t("•••••••• (stored, leave blank to keep)")
            : "glpat-… / gldt-…";
    }

    /**
     * The label of a token kind.
     *
     * @param {string} kind one of CREDENTIAL_KINDS
     * @returns {string}
     */
    kindLabel(kind) {
        return this.kindOptions.find((o) => o.value === kind)?.label || kind;
    }

    /** Open the editor on a new provider of gitlab.com. */
    addProvider() {
        this.state.saveError = null;
        this.state.draft = {
            id: false, name: "GitLab.com", base_url: "https://gitlab.com",
            ca_bundle: "", credentials: [blankCredential()],
        };
    }

    /**
     * Open the editor on a copy of *provider*.
     *
     * @param {Object} provider a provider as the panel lists it
     */
    editProvider(provider) {
        this.state.saveError = null;
        this.state.draft = {
            id: provider.id,
            name: provider.name,
            base_url: provider.base_url,
            ca_bundle: provider.ca_bundle || "",
            credentials: (provider.credentials || []).map((c) => ({
                ...c, token: "", expires_on: c.expires_on || "",
            })),
        };
    }

    /** Close the editor without saving. */
    cancelEdit() {
        this.state.draft = null;
        this.state.saveError = null;
    }

    /** Add an empty credential row to the editor. */
    addCredential() {
        this.state.draft.credentials.push(blankCredential());
    }

    /**
     * Remove the credential row *index* from the editor.
     *
     * @param {number} index the row
     */
    removeCredential(index) {
        this.state.draft.credentials.splice(index, 1);
    }

    /**
     * Save the provider being edited, with its credentials.
     *
     * @returns {Promise<void>}
     */
    async save() {
        const draft = this.state.draft;
        this.state.saving = true;
        this.state.saveError = null;
        try {
            const res = await rpc("/cloud/save_git_provider", {
                vals: {
                    id: draft.id || false,
                    name: draft.name,
                    base_url: draft.base_url,
                    ca_bundle: draft.ca_bundle,
                    credentials: draft.credentials.map((c) => ({
                        id: c.id || false,
                        namespace: c.namespace,
                        kind: c.kind,
                        username: c.kind === "deploy" ? c.username : "",
                        token: c.token,
                        expires_on: c.expires_on || false,
                    })),
                },
            });
            if (!res.ok) {
                this.state.saveError = res.error || _t("Could not save the Git provider.");
                return;
            }
            this.state.warnings[res.provider.id] = res.warnings || [];
            this.state.draft = null;
            await this.load();
        } catch {
            this.state.saveError = _t("Could not reach the server.");
        } finally {
            this.state.saving = false;
        }
    }

    /**
     * Delete *provider*, after asking.
     *
     * @param {Object} provider a provider as the panel lists it
     * @returns {Promise<void>}
     */
    async deleteProvider(provider) {
        const ok = await confirmVia(this.state, {
            title: _t("Delete Git provider"),
            message: _t(
                "Delete %(name)s and its credentials? Repository lines that use it must be changed first.",
                { name: provider.name },
            ),
            confirmLabel: _t("Delete"),
            isDanger: true,
        });
        if (!ok) {
            return;
        }
        delete this.state.deleteError[provider.id];
        try {
            const res = await rpc("/cloud/delete_git_provider", { provider_id: provider.id });
            if (!res.ok) {
                this.state.deleteError[provider.id] = res.error;
                return;
            }
        } catch {
            this.state.deleteError[provider.id] = _t("Could not reach the server.");
            return;
        }
        await this.load();
    }

    /**
     * Test *provider* with the repository URL typed for it.
     *
     * @param {Object} provider a provider as the panel lists it
     * @returns {Promise<void>}
     */
    async testProvider(provider) {
        const url = (this.state.testUrl[provider.id] || "").trim();
        if (!url) {
            this.state.testResult[provider.id] = {
                error: _t("Type the URL of a repository of this server."),
            };
            return;
        }
        this.state.testing = provider.id;
        try {
            const res = await rpc("/cloud/test_git_provider", {
                provider_id: provider.id, url,
            });
            this.state.testResult[provider.id] = res.ok
                ? { git: res.git, api: res.api }
                : { error: res.error };
            if (res.ok) {
                const index = this.state.providers.findIndex((p) => p.id === provider.id);
                if (index >= 0) {
                    this.state.providers[index] = res.provider;
                }
            }
        } catch {
            this.state.testResult[provider.id] = { error: _t("Could not reach the server.") };
        } finally {
            this.state.testing = null;
        }
    }
}
