import { Component, useState, useSubEnv, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { CoreRatesTab } from "../core_rates_tab/core_rates_tab";
import { RlSelect } from "../rl_select/rl_select";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";
import { confirmVia } from "../../utils/use_confirm";

export class Settings extends Component {
    static template = "incubacloud.Settings";
    static components = { IcConfirmDialog, CoreRatesTab, RlSelect };
    static props = {
        initialTab: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            tab: this.props.initialTab || "general",
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
            // Observability: the token is write-only, so only its
            // presence is tracked client-side.
            hasMetricsToken: false,
            // Advanced metrics fields stay folded: with the button doing
            // the wiring, a single-host setup never needs them, and six
            // visible fields read as six decisions to make.
            showAdvancedMetrics: false,
            deployingCentral: false,
            centralHostId: null,
            hosts: [],
            form: {
                // General
                autoassign_enabled: false,
                default_backup_backend_id: null,
                audit_log_retention_days: 90,
                job_log_retention_days: 30,
                job_retention_days: 180,
                default_backup_alert_threshold_pct: 80,
                github_event_retention_days: 90,
                github_event_truncate_days: 7,
                // Container log rotation (per service, in the compose override)
                container_log_max_size: "10m",
                container_log_max_file: 3,
                odoo_log_archive_days: 60,
                log_download_max_mb: 64,
                log_search_max_files: 60,
                log_search_timeout_s: 30,
                // Edge
                trusted_proxy_ranges: "",
                github_webhook_allowlist: false,
                panel_host_id: null,
                panel_hostname: "",
                panel_service_url: "",
                panel_tls_domain: "",
                // GitHub
                app_id: "",
                installation_id: "",
                webhook_secret: "",
                private_key: "",
                github_pat: "",
            },
            // Read-only, derived server-side: what the proxy ranges and
            // the panel route actually resolve to, and where from. Kept
            // out of `form` so they are never sent back on save.
            effectiveProxyRanges: [],
            trustedProxySource: "none",
            panelRoute: {},
            panelRouteSource: "none",
            panelRouteHostId: null,
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
            job_retention_days: [
                nonNegativeInt(_t("Must be a positive integer")),
            ],
            github_event_retention_days: [
                nonNegativeInt(_t("Must be a positive integer")),
            ],
            github_event_truncate_days: [
                nonNegativeInt(_t("Must be a positive integer")),
            ],
            // Docker's max-size grammar, one canonical spelling; the
            // server lower-cases and trims, so "10M " is fine to type.
            container_log_max_size: [
                (v) => (/^[1-9]\d*[kmg]$/i.test(String(v ?? "").trim())
                    ? null
                    : _t("Use a positive integer followed by k, m or g (e.g. 10m)")),
            ],
            container_log_max_file: [
                (v) => {
                    const n = Number(v);
                    return (Number.isInteger(n) && n >= 1) ? null : _t("Must be at least 1");
                },
            ],
            odoo_log_archive_days: [
                (v) => {
                    const n = Number(v);
                    return (Number.isInteger(n) && n >= 1) ? null : _t("Must be at least 1 day");
                },
            ],
            log_download_max_mb: [
                (v) => {
                    const n = Number(v);
                    return (Number.isInteger(n) && n >= 1) ? null : _t("Must be at least 1 MB");
                },
            ],
            log_search_max_files: [
                (v) => {
                    const n = Number(v);
                    return (Number.isInteger(n) && n >= 1) ? null : _t("Must be at least 1 day");
                },
            ],
            log_search_timeout_s: [
                (v) => {
                    const n = Number(v);
                    return (Number.isInteger(n) && n >= 5) ? null : _t("Must be at least 5 seconds");
                },
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

    /**
     * Backup backend options for the RlSelect, derived from loaded backends.
     * @returns {{value: number, label: string}[]} option list (numeric ids preserved)
     */
    get backupBackendOptions() {
        return this.state.backupBackends.map((bb) => ({ value: bb.id, label: bb.name }));
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

    /**
     * Describe where the effective proxy ranges come from.
     *
     * @returns {string} a phrase for the read-only line, or "".
     */
    proxySourceLabel() {
        return {
            settings: _t("the override below"),
            cloudflare: _t("the platform's CDN, refreshed daily"),
        }[this.state.trustedProxySource] || "";
    }

    /**
     * Whether the panel's webhook route is derived rather than typed.
     *
     * @returns {boolean} true when an upper layer supplies it, in which
     *   case the fields are shown read-only.
     */
    panelRouteDerived() {
        return this.state.panelRouteSource === "catchall";
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
            this.state.form.metrics_enabled = !!general.metrics_enabled;
            this.state.form.metrics_central_url = general.metrics_central_url || "";
            this.state.form.metrics_remote_write_url = general.metrics_remote_write_url || "";
            this.state.form.metrics_remote_write_token = "";
            this.state.form.metrics_retention_days = general.metrics_retention_days || 90;
            this.state.form.grafana_base_url = general.grafana_base_url || "";
            this.state.hasMetricsToken = !!general.has_metrics_remote_write_token;
            this.state.form.trusted_proxy_ranges = general.trusted_proxy_ranges || "";
            this.state.form.github_webhook_allowlist = !!general.github_webhook_allowlist;
            this.state.form.panel_host_id = general.panel_host_id || null;
            this.state.form.panel_hostname = general.panel_hostname || "";
            this.state.form.panel_service_url = general.panel_service_url || "";
            this.state.form.panel_tls_domain = general.panel_tls_domain || "";
            this.state.effectiveProxyRanges =
                general.effective_trusted_proxy_ranges || [];
            this.state.trustedProxySource = general.trusted_proxy_source || "none";
            this.state.panelRoute = general.panel_route || {};
            this.state.panelRouteSource = general.panel_route_source || "none";
            this.state.panelRouteHostId = general.panel_route_host_id || null;
            // Hosts for the central's destination picker. Non-fatal: the
            // rest of Settings must still work if this one call fails.
            try {
                const hres = await rpc("/cloud/get_hosts", {});
                this.state.hosts = hres.hosts || hres || [];
            } catch {
                this.state.hosts = [];
            }
            this.state.hasWebhookSecret = data.has_webhook_secret || false;
            this.state.hasPat = data.has_pat || false;
            this.state.slug = data.slug || "";
            this.state.installUrl = data.install_url || "";
            this.state.form.autoassign_enabled = general.autoassign_enabled || false;
            this.state.form.default_backup_backend_id = general.default_backup_backend_id || null;
            this.state.form.audit_log_retention_days = general.audit_log_retention_days ?? 90;
            this.state.form.job_log_retention_days = general.job_log_retention_days ?? 30;
            this.state.form.job_retention_days = general.job_retention_days ?? 180;
            this.state.form.default_backup_alert_threshold_pct = general.default_backup_alert_threshold_pct ?? 80;
            this.state.form.github_event_retention_days = general.github_event_retention_days ?? 90;
            this.state.form.github_event_truncate_days = general.github_event_truncate_days ?? 7;
            this.state.form.container_log_max_size = general.container_log_max_size || "10m";
            this.state.form.container_log_max_file = general.container_log_max_file || 3;
            this.state.form.odoo_log_archive_days = general.odoo_log_archive_days || 60;
            this.state.form.log_download_max_mb = general.log_download_max_mb || 64;
            this.state.form.log_search_max_files = general.log_search_max_files || 60;
            this.state.form.log_search_timeout_s = general.log_search_timeout_s || 30;
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

    /** Options for the central's destination host picker. */
    get centralHostOptions() {
        return this.state.hosts.map((h) => ({ value: h.id, label: h.name }));
    }

    /**
     * Queue the metrics central deployment on the chosen host.
     *
     * The destination is a parameter rather than a fixed host, which is
     * what makes "co-locate now, move to its own VPS later" a re-run
     * instead of a migration.
     */
    /**
     * One line describing where observability currently stands.
     *
     * The form used to show six fields and no answer to "is this
     * working?". With enrolment automatic, that question is the only one
     * an operator actually has on this screen.
     */
    get observabilityStatus() {
        if (!this.state.form.metrics_enabled) {
            return _t("Off. Pick a host above and enable it.");
        }
        if (!this.state.form.metrics_central_url) {
            return _t("On, but no query endpoint is set — nothing can be read back.");
        }
        if (!this.state.form.grafana_base_url) {
            return _t("Collecting. Charts are hidden until a Grafana URL is set.");
        }
        return _t("Collecting, with charts available.");
    }

    async deployCentral() {
        if (this.state.deployingCentral) return;
        const hostId = this.state.centralHostId;
        if (!hostId) {
            this.env.toast?.error(_t("Pick the host that should run it."));
            return;
        }
        this.state.deployingCentral = true;
        try {
            const res = await rpc("/cloud/monitoring/deploy_central", {
                host_id: hostId,
            });
            if (res?.ok) {
                this.env.toast?.success(_t("Deployment queued."));
            } else {
                this.env.toast?.error(res?.error || _t("Could not queue it."));
            }
        } catch (e) {
            this.env.toast?.error(
                e.data?.message || e.message || _t("Could not queue it."),
            );
        } finally {
            this.state.deployingCentral = false;
        }
    }

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
            const res = await rpc("/cloud/save_general_settings", {
                autoassign_enabled: this.state.form.autoassign_enabled,
                default_backup_backend_id: this.state.form.default_backup_backend_id,
                audit_log_retention_days: this.state.form.audit_log_retention_days,
                job_log_retention_days: this.state.form.job_log_retention_days,
                job_retention_days: this.state.form.job_retention_days,
                default_backup_alert_threshold_pct: this.state.form.default_backup_alert_threshold_pct,
                github_event_retention_days: this.state.form.github_event_retention_days,
                github_event_truncate_days: this.state.form.github_event_truncate_days,
                container_log_max_size: this.state.form.container_log_max_size,
                container_log_max_file: this.state.form.container_log_max_file,
                odoo_log_archive_days: this.state.form.odoo_log_archive_days,
                log_download_max_mb: this.state.form.log_download_max_mb,
                log_search_max_files: this.state.form.log_search_max_files,
                log_search_timeout_s: this.state.form.log_search_timeout_s,
                metrics_enabled: this.state.form.metrics_enabled,
                metrics_central_url: this.state.form.metrics_central_url,
                metrics_remote_write_url: this.state.form.metrics_remote_write_url,
                metrics_remote_write_token: this.state.form.metrics_remote_write_token,
                metrics_retention_days: this.state.form.metrics_retention_days,
                grafana_base_url: this.state.form.grafana_base_url,
                trusted_proxy_ranges: this.state.form.trusted_proxy_ranges,
                github_webhook_allowlist: this.state.form.github_webhook_allowlist,
                // Only sent when this installation describes its own
                // panel. Where a layer above derives the route, the
                // fields are read-only and echoing them back would
                // freeze a value that is meant to keep following it.
                ...(this.panelRouteDerived() ? {} : {
                    panel_host_id: this.state.form.panel_host_id,
                    panel_hostname: this.state.form.panel_hostname,
                    panel_service_url: this.state.form.panel_service_url,
                    panel_tls_domain: this.state.form.panel_tls_domain,
                }),
            });
            if (res && res.ok === false) {
                // The range list is refused by a model constraint rather
                // than trimmed, so the message names the bad entries.
                this.env.toast?.error(res.error || _t("General settings"));
                errors.push(_t("General settings"));
            }
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

    /** Open the shared confirmation dialog. See utils/use_confirm.js. */
    _confirm(opts) {
        return confirmVia(this.state, opts);
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
