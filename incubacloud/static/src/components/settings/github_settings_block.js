/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, status, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { confirmVia } from "../../utils/use_confirm";
import { SettingAdvanced, SettingField, SettingsBlock } from "../settings_form/settings_form";

/** The App's fields, saved together through /cloud/save_github_app. */
export const GITHUB_APP_FIELDS = Object.freeze([
    "app_id", "installation_id", "webhook_secret", "private_key",
]);
/** Secrets: the server keeps them and only says whether one is set. */
const WRITE_ONLY = ["webhook_secret", "private_key", "github_pat"];

/**
 * Settings → GitHub: the GitHub App that rebuilds on push and reads
 * repositories, and a personal access token for what the App cannot reach.
 *
 * Until the App exists the block walks through creating and installing
 * it; once connected it says so and offers the checks. The App's IDs and
 * secrets stay under Advanced: the setup fills them in.
 */
export class GithubSettingsBlock extends Component {
    static template = "incubacloud.GithubSettingsBlock";
    static components = { IcConfirmDialog, SettingAdvanced, SettingField, SettingsBlock };
    static props = {};

    /** Read the App's state; join the save bar once mounted. */
    setup() {
        this.loaded = false;
        this.state = useState({
            loading: true,
            loadError: null,
            configured: false,
            hasPrivateKey: false,
            hasWebhookSecret: false,
            hasPat: false,
            slug: "",
            installUrl: "",
            isHttps: window.location.protocol === "https:",
            testing: false,
            // The answer of Test connection or Re-detect, under the buttons.
            actionMsg: null,
            confirmDialog: null,
            form: {
                app_id: "", installation_id: "", webhook_secret: "", private_key: "", github_pat: "",
            },
        });
        onWillStart(() => this.load());
        onMounted(() => {
            this._register();
            this._checkUrlParams();
        });
        onWillUnmount(() => this.env.unregisterSettingsSave?.("GitHub"));
    }

    /** Read the App and the token's state. */
    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const data = await rpc("/cloud/get_github_app", {});
            Object.assign(this.state, {
                configured: Boolean(data.configured),
                hasPrivateKey: Boolean(data.has_private_key),
                hasWebhookSecret: Boolean(data.has_webhook_secret),
                hasPat: Boolean(data.has_pat),
                slug: data.slug || "",
                installUrl: data.install_url || "",
            });
            Object.assign(this.state.form, {
                app_id: data.app_id || "",
                installation_id: data.installation_id || "",
                webhook_secret: "",
                private_key: "",
                github_pat: "",
            });
            this.loaded = true;
        } catch {
            this.state.loadError = _t("Failed to load the GitHub settings.");
        } finally {
            this.state.loading = false;
        }
    }

    /** Join the page's save bar, once loaded and while mounted. */
    _register() {
        if (this.loaded && status(this) === "mounted") {
            this.env.registerSettingsSave?.("GitHub", () => this._save(), this.state, WRITE_ONLY);
        }
    }

    /** Load again after a failure, and join the save bar if it worked. */
    async reload() {
        await this.load();
        this._register();
    }

    /** Say how the App's creation on GitHub went, when it sent us back here. */
    _checkUrlParams() {
        const params = new URLSearchParams(window.location.search);
        if (params.get("setup_ok") === "1") {
            this.env.toast?.success(_t("GitHub App created! Now install it on your organization to complete the setup."));
        } else if (params.has("setup_error")) {
            const code = params.get("setup_error") || "unknown";
            if (code === "requires_https") {
                this.env.toast?.info(_t("Automatic setup requires HTTPS. Create the GitHub App by hand and paste its credentials under Advanced."));
            } else {
                this.env.toast?.error(_t("GitHub App setup failed (%s). Try again, or fill it in by hand under Advanced.", code));
            }
        } else {
            return;
        }
        history.replaceState(null, "", window.location.pathname);
    }

    /** @returns {number} 1 create the App, 2 install it, 3 connected */
    get currentStep() {
        if (!this.state.configured) {
            return 1;
        }
        return this.state.form.installation_id ? 3 : 2;
    }

    /** @returns {string} the status chip of a connected App */
    get statusText() {
        return _t("Connected · App ID %s", this.state.form.app_id);
    }

    /**
     * Save what changed: the App's fields, the token, or both.
     *
     * Overwriting stored App credentials asks first, since the old ones
     * stop working at once.
     *
     * @returns {Promise<{ok: boolean, error?: string}>}
     */
    async _save() {
        const form = this.state.form;
        const saved = this.env.settingsForm?.store?.saved?.GitHub || {};
        const appChanged = GITHUB_APP_FIELDS.some(
            (key) => JSON.stringify(form[key]) !== saved[key],
        );
        if (appChanged && form.app_id) {
            const vals = (force) => ({
                app_id: form.app_id,
                installation_id: form.installation_id,
                webhook_secret: form.webhook_secret,
                private_key: form.private_key,
                ...(force ? { force: true } : {}),
            });
            let result = await rpc("/cloud/save_github_app", { vals: vals(false) });
            if (!result.ok && result.requires_force) {
                const confirmed = await this._confirm({
                    title: _t("Overwrite GitHub App credentials?"),
                    message: _t(
                        "You are about to overwrite the stored %s. The previous credentials stop working at once, and this cannot be undone.",
                        result.requires_force.join(", "),
                    ),
                    confirmLabel: _t("Overwrite"),
                    isDanger: true,
                });
                if (!confirmed) {
                    return { ok: false, error: _t("The GitHub App's credentials were kept.") };
                }
                result = await rpc("/cloud/save_github_app", { vals: vals(true) });
            }
            if (!result.ok) {
                return { ok: false, error: result.error || _t("The GitHub App was not saved.") };
            }
            this.state.configured = true;
            if (form.private_key) {
                this.state.hasPrivateKey = true;
            }
            if (form.webhook_secret) {
                this.state.hasWebhookSecret = true;
            }
            form.private_key = "";
            form.webhook_secret = "";
        }
        const pat = (form.github_pat || "").trim();
        if (pat) {
            await rpc("/cloud/save_github_pat", { pat });
            this.state.hasPat = true;
            form.github_pat = "";
        }
        return { ok: true };
    }

    /** Start creating the App on GitHub, which sends the browser back here. */
    createOnGitHub() {
        window.location.href = "/cloud/github/setup";
    }

    /** Open GitHub's page to install the App on an organization. */
    openInstallUrl() {
        window.open(this.state.installUrl || "https://github.com/settings/installations", "_blank");
    }

    /**
     * "Check installation": read again and, when the webhook took no
     * installation, ask GitHub for them.
     */
    async refresh() {
        await this.load();
        if (!this.state.form.installation_id) {
            await this.detectInstallation();
        }
        if (this.state.form.installation_id) {
            this.env.toast?.success(_t("Installation detected! Your GitHub App is now fully connected."));
        } else {
            this.env.toast?.info(_t("Installation not detected yet. Install the app on your GitHub organization and try again."));
        }
    }

    /** Ask GitHub which installation the App has; saved when there is one. */
    async detectInstallation() {
        this.state.actionMsg = null;
        try {
            const result = await rpc("/cloud/detect_github_installation", {});
            if (result.ok && result.auto_updated) {
                this.state.form.installation_id = result.installation_id;
                const saved = this.env.settingsForm?.store?.saved?.GitHub;
                if (saved) {
                    saved.installation_id = JSON.stringify(result.installation_id);
                }
                this.state.actionMsg = {
                    type: "success",
                    message: _t("Installation detected: %s (%s). Saved.", result.installation_id, result.account),
                };
            } else if (result.ok && result.installations) {
                const list = result.installations
                    .map((i) => `${i.account_login} (ID: ${i.id})`)
                    .join(", ");
                this.state.actionMsg = {
                    type: "info",
                    message: _t("Several installations found: %s. Type the right Installation ID under Advanced.", list),
                };
            } else {
                this.state.actionMsg = { type: "error", message: result.error || _t("Detection failed.") };
            }
        } catch {
            this.state.actionMsg = { type: "error", message: _t("Unexpected error during detection.") };
        }
    }

    /** Check the stored credentials against GitHub. */
    async testConnection() {
        this.state.testing = true;
        this.state.actionMsg = null;
        try {
            const result = await rpc("/cloud/test_github_connection", {});
            this.state.actionMsg = result.ok
                ? { type: "success", message: _t("Connected. App: %s (ID: %s)", result.app_name || "", result.app_id || "") }
                : { type: "error", message: result.error || _t("Connection failed.") };
        } catch {
            this.state.actionMsg = { type: "error", message: _t("Unexpected error during connection test.") };
        } finally {
            this.state.testing = false;
        }
    }

    /** Delete the App's configuration here, after asking. */
    async disconnectApp() {
        const ok = await this._confirm({
            title: _t("Disconnect GitHub App"),
            message: _t("This deletes the current GitHub App configuration. You will need to create or configure a new app to restore the integration. Continue?"),
            confirmLabel: _t("Disconnect"),
            isDanger: true,
        });
        if (!ok) {
            return;
        }
        try {
            await rpc("/cloud/reset_github_app", {});
            this.state.actionMsg = null;
            await this.reload();
        } catch {
            this.env.toast?.error(_t("Failed to disconnect the app."));
        }
    }

    /** Open the block's confirmation dialog. See utils/use_confirm.js. */
    _confirm(opts) {
        return confirmVia(this.state, opts);
    }
}
