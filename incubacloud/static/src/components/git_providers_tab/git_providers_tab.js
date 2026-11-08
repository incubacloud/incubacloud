/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { RlSelect } from "../rl_select/rl_select";
import { confirmVia } from "../../utils/use_confirm";

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

/**
 * Settings → Git providers: the GitLab servers the panel reads, deploys
 * and rebuilds from, and their credentials (plan-gitlab.md §4.11-1).
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
        });
        onWillStart(() => this.load());
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
