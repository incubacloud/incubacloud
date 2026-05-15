import { Component, useState, useSubEnv, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { CoreRatesTab } from "../core_rates_tab/core_rates_tab";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";

export class Settings extends Component {
    static template = "incubacloud.Settings";
    static components = { IcConfirmDialog, CoreRatesTab };
    static props = {};

    setup() {
        this.state = useState({
            tab: "general",
            loading: true,
            saving: false,
            testing: false,
            hasPrivateKey: false,
            hasWebhookSecret: false,
            hasPat: false,
            configured: false,
            isHttps: window.location.protocol === "https:",
            slug: "",
            installUrl: "",
            showManual: false,
            backupBackends: [],
            form: {
                // General
                autoassign_enabled: false,
                default_backup_backend_id: null,
                audit_log_retention_days: 90,
                job_log_retention_days: 30,
                default_backup_alert_threshold_pct: 80,
                // GitHub
                app_id: "",
                installation_id: "",
                webhook_secret: "",
                private_key: "",
                github_pat: "",
            },
            // Inline feedback for GitHub action buttons (test, detect)
            ghActionMsg: null,
            confirmDialog: null,
            auditPurging: false,
        });

        // Save registry: child tab components register their save handlers
        const _saveHandlers = new Map();
        useSubEnv({
            registerSettingsSave: (key, handler) => _saveHandlers.set(key, handler),
            unregisterSettingsSave: (key) => _saveHandlers.delete(key),
        });
        this._saveHandlers = _saveHandlers;
        this._savedForm = null;

        // Retention fields are numeric and can reach the controller
        // as strings from <input type=number>; reject negative/NaN
        // client-side so the server isn't the one rendering the error.
        const nonNegativeInt = (msg) => (v) => {
            if (v == null || String(v).trim() === "") return null;
            const n = Number(v);
            return (Number.isInteger(n) && n >= 0) ? null : msg;
        };
        this.validator = useFormValidation(() => ({
            audit_log_retention_days: [
                nonNegativeInt(_t("Must be a positive integer")),
            ],
            job_log_retention_days: [
                nonNegativeInt(_t("Must be a positive integer")),
            ],
        }));

        onWillStart(() => this.loadConfig());
        onMounted(() => {
            this._checkUrlParams();
        });
        useNavGuard(
            () => this.hasUnsavedChanges,
            (opts) => this._confirm(opts),
        );
    }

    get hasUnsavedChanges() {
        return this._savedForm && JSON.stringify(this.state.form) !== this._savedForm;
    }

    _checkUrlParams() {
        const params = new URLSearchParams(window.location.search);
        if (params.get("setup_ok") === "1") {
            this.state.tab = "github";
            this.env.toast?.success(_t("GitHub App created! Now install it on your organization to complete the setup."));
            history.replaceState(null, "", window.location.pathname);
        } else if (params.has("setup_error")) {
            const code = params.get("setup_error") || "unknown";
            const msg = code === "requires_https"
                ? _t("Automatic setup requires HTTPS. Please create the GitHub App manually and paste the credentials below.")
                : _t("GitHub App setup failed (%s). Please try again or configure manually.").replace('%s', code);
            this.state.tab = "github";
            this.env.toast?.[code === "requires_https" ? "info" : "error"](msg);
            history.replaceState(null, "", window.location.pathname);
        }
    }

    async loadConfig() {
        this.state.loading = true;
        try {
            const [data, general, backends] = await Promise.all([
                rpc("/cloud/get_github_app", {}),
                rpc("/cloud/get_general_settings", {}),
                rpc("/cloud/get_backup_backends", {}),
            ]);
            this.state.configured = data.configured || false;
            this.state.form.app_id = data.app_id || "";
            this.state.form.installation_id = data.installation_id || "";
            this.state.form.webhook_secret = "";
            this.state.form.private_key = "";
            this.state.form.github_pat = "";
            this.state.hasPrivateKey = data.has_private_key || false;
            this.state.hasWebhookSecret = data.has_webhook_secret || false;
            this.state.hasPat = data.has_pat || false;
            this.state.slug = data.slug || "";
            this.state.installUrl = data.install_url || "";
            this.state.form.autoassign_enabled = general.autoassign_enabled || false;
            this.state.form.default_backup_backend_id = general.default_backup_backend_id || null;
            this.state.form.audit_log_retention_days = general.audit_log_retention_days ?? 90;
            this.state.form.job_log_retention_days = general.job_log_retention_days ?? 30;
            this.state.form.default_backup_alert_threshold_pct = general.default_backup_alert_threshold_pct ?? 80;
            this.state.backupBackends = backends?.items || backends || [];
            this._savedForm = JSON.stringify(this.state.form);
        } catch {
            this.env.toast?.error(_t("Failed to load configuration."));
        } finally {
            this.state.loading = false;
        }
    }

    get currentStep() {
        if (!this.state.configured) return 1;
        if (!this.state.form.installation_id) return 2;
        return 3;
    }

    async refresh() {
        await this.loadConfig();
        if (this.state.form.installation_id) {
            this.env.toast?.success(_t("Installation detected! Your GitHub App is now fully connected."));
        } else {
            this.env.toast?.info(_t("Installation not detected yet. Install the app on your GitHub organization and try again."));
        }
    }

    onInput(field, ev) {
        this.state.form[field] = ev.target.value;
    }

    onIntInput(field, ev) {
        this.state.form[field] = parseInt(ev.target.value, 10) || 0;
    }

    createOnGitHub() {
        window.location.href = "/cloud/github/setup";
    }

    openInstallUrl() {
        const url = this.state.installUrl || "https://github.com/settings/installations";
        window.open(url, "_blank");
    }

    // ── Unified save ────────────────────────────────────────────────────

    async save() {
        if (this.state.saving) return;
        const { isValid, firstError } = this.validator.validate(this.state.form);
        if (!isValid) {
            this.env.toast?.error(firstError);
            return;
        }
        this.state.saving = true;
        const errors = [];

        // General settings
        try {
            await rpc("/cloud/save_general_settings", {
                autoassign_enabled: this.state.form.autoassign_enabled,
                default_backup_backend_id: this.state.form.default_backup_backend_id,
                audit_log_retention_days: this.state.form.audit_log_retention_days,
                job_log_retention_days: this.state.form.job_log_retention_days,
                default_backup_alert_threshold_pct: this.state.form.default_backup_alert_threshold_pct,
            });
        } catch {
            errors.push(_t("General settings"));
        }

        // GitHub App (only if configured / has app_id)
        if (this.state.form.app_id) {
            try {
                const buildVals = (force) => ({
                    app_id: this.state.form.app_id,
                    installation_id: this.state.form.installation_id,
                    webhook_secret: this.state.form.webhook_secret,
                    private_key: this.state.form.private_key,
                    ...(force ? { force: true } : {}),
                });
                let result = await rpc("/cloud/save_github_app", { vals: buildVals(false) });
                if (!result.ok && result.requires_force) {
                    const confirmed = await this._confirm({
                        title: _t("Overwrite GitHub App credentials?"),
                        message: _t(
                            "You are about to overwrite the existing %s. " +
                            "This invalidates the previous credentials and " +
                            "cannot be undone. Continue?"
                        ).replace('%s', result.requires_force.join(', ')),
                        confirmLabel: _t("Overwrite"),
                        isDanger: true,
                    });
                    if (!confirmed) {
                        result = { ok: true, cancelled: true };
                    } else {
                        result = await rpc("/cloud/save_github_app", { vals: buildVals(true) });
                    }
                }
                if (result.ok && !result.cancelled) {
                    this.state.hasPrivateKey = true;
                    this.state.configured = true;
                    this.state.form.private_key = "";
                } else if (result.error) {
                    errors.push(result.error);
                }
            } catch {
                errors.push(_t("GitHub App"));
            }
        }

        // GitHub PAT (only if a new value was entered)
        const pat = (this.state.form.github_pat || "").trim();
        if (pat) {
            try {
                await rpc("/cloud/save_github_pat", { pat });
                this.state.hasPat = true;
                this.state.form.github_pat = "";
            } catch {
                errors.push(_t("GitHub PAT"));
            }
        }

        // Registered save handlers from inheriting modules
        for (const [key, handler] of this._saveHandlers) {
            try {
                const result = await handler();
                if (result && !result.ok && result.error) {
                    errors.push(result.error);
                }
            } catch {
                errors.push(key);
            }
        }

        this.state.saving = false;
        if (errors.length) {
            this.env.toast?.error(_t("Failed to save: %s").replace('%s', errors.join(", ")));
        } else {
            this.env.toast?.success(_t("Changes saved"));
            this._savedForm = JSON.stringify(this.state.form);
        }
    }

    // ── General tab ─────────────────────────────────────────────────────

    toggleAutoassign() {
        this.state.form.autoassign_enabled = !this.state.form.autoassign_enabled;
        this.state.saved = false;
    }

    onBackendChange(ev) {
        const val = ev.target.value;
        this.state.form.default_backup_backend_id = val ? parseInt(val, 10) : null;
        this.state.saved = false;
    }

    // ── GitHub tab actions (inline feedback, not save) ──────────────────

    async detectInstallation() {
        this.state.ghActionMsg = null;
        try {
            const result = await rpc("/cloud/detect_github_installation", {});
            if (result.ok && result.auto_updated) {
                this.state.form.installation_id = result.installation_id;
                this.state.ghActionMsg = {
                    type: "success",
                    message: _t("Installation detected: %s (%s). Saved automatically.").replace('%s', result.installation_id).replace('%s', result.account),
                };
            } else if (result.ok && result.installations) {
                const list = result.installations
                    .map(i => `${i.account_login} (ID: ${i.id})`)
                    .join(", ");
                this.state.ghActionMsg = {
                    type: "info",
                    message: _t("Multiple installations found: %s. Enter the correct ID manually.").replace('%s', list),
                };
                this.state.showManual = true;
            } else {
                this.state.ghActionMsg = { type: "error", message: result.error || _t("Detection failed.") };
            }
        } catch {
            this.state.ghActionMsg = { type: "error", message: _t("Unexpected error during detection.") };
        }
    }

    async testConnection() {
        this.state.testing = true;
        this.state.ghActionMsg = null;
        try {
            const result = await rpc("/cloud/test_github_connection", {});
            if (result.ok) {
                this.state.ghActionMsg = {
                    type: "success",
                    message: _t("Connected — App: %s (ID: %s)").replace('%s', result.app_name || "").replace('%s', result.app_id || ""),
                };
            } else {
                this.state.ghActionMsg = { type: "error", message: result.error || _t("Connection failed.") };
            }
        } catch {
            this.state.ghActionMsg = { type: "error", message: _t("Unexpected error during connection test.") };
        } finally {
            this.state.testing = false;
        }
    }

    // ── Shared utilities ────────────────────────────────────────────────

    setTab(tab) {
        this.state.tab = tab;
        this.state.ghActionMsg = null;
    }

    toggleManual() {
        this.state.showManual = !this.state.showManual;
    }

    _confirm({ title, message, confirmLabel = _t("Confirm"), isDanger = false }) {
        return new Promise((resolve) => {
            this.state.confirmDialog = {
                title, message, confirmLabel, isDanger,
                onConfirm: () => { this.state.confirmDialog = null; resolve(true); },
                onCancel:  () => { this.state.confirmDialog = null; resolve(false); },
            };
        });
    }

    async purgeAuditLogs() {
        this.state.auditPurging = true;
        try {
            const result = await rpc("/cloud/purge_audit_logs", {
                days: this.state.form.audit_log_retention_days,
            });
            if (result.ok) {
                this.env.toast?.success(
                    result.deleted
                        ? _t("Deleted %s old audit log entries").replace('%s', result.deleted)
                        : _t("No old entries to purge")
                );
            }
        } catch {
            this.env.toast?.error(_t("Failed to purge audit logs"));
        } finally {
            this.state.auditPurging = false;
        }
    }

    async disconnectApp() {
        const ok = await this._confirm({
            title: _t("Disconnect GitHub App"),
            message: _t("This will delete the current GitHub App configuration. You will need to create or configure a new app to restore the integration. Continue?"),
            confirmLabel: _t("Disconnect"),
            isDanger: true,
        });
        if (!ok) return;
        try {
            await rpc("/cloud/reset_github_app", {});
            this.state.configured = false;
            this.state.hasPrivateKey = false;
            this.state.hasPat = false;
            this.state.slug = "";
            this.state.installUrl = "";
            this.state.form.app_id = "";
            this.state.form.installation_id = "";
            this.state.form.webhook_secret = "";
            this.state.form.private_key = "";
            this.state.form.github_pat = "";
            this.state.showManual = false;
        } catch {
            this.state.error = _t("Failed to disconnect the app.");
        }
    }
}
