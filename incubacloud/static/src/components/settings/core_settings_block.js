/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, status, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { announceJob } from "../../utils/job_queue";
import { GitProvidersTab } from "../git_providers_tab/git_providers_tab";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { confirmVia } from "../../utils/use_confirm";
import { RlSelect } from "../rl_select/rl_select";
import {
    SettingAdvanced, SettingField, SettingRow, SettingsBlock,
} from "../settings_form/settings_form";

/**
 * The fields each part of the panel's own settings holds, by the section it
 * is drawn on. They all travel through /cloud/get_general_settings and
 * /cloud/save_general_settings, which writes only what it is sent.
 */
export const CORE_PARTS = Object.freeze({
    general: [
        "autoassign_enabled", "default_backup_backend_id",
        "default_backup_alert_threshold_pct",
    ],
    instances: [
        "move_frozen_source_hours", "move_cut_alert_seconds", "staging_autopurge_days",
    ],
    logs: [
        "audit_log_retention_days", "job_retention_days", "job_log_retention_days",
        "host_session_retention_days",
        "github_event_truncate_days", "github_event_retention_days",
        "odoo_log_archive_days", "container_log_max_size", "container_log_max_file",
        "log_download_max_mb", "log_search_max_files", "log_search_timeout_s",
        "cron_heartbeat_url",
    ],
    edge: ["trusted_proxy_ranges"],
    monitoring: [
        "metrics_enabled", "metrics_central_url", "metrics_remote_write_url",
        "metrics_remote_write_token", "metrics_retention_days",
        "metrics_collect_pg",
        "metrics_central_public_host", "metrics_central_direct_host",
        "acme_dns_cloudflare_token", "metrics_central_operator_sources",
        "metrics_central_single_path", "metrics_central_write_access",
    ],
});

/** Secrets: the server keeps them and only says whether one is set. */
const WRITE_ONLY = ["metrics_remote_write_token", "acme_dns_cloudflare_token"];

/** What each field holds when the server has nothing for it. */
const DEFAULTS = {
    autoassign_enabled: false,
    default_backup_backend_id: null,
    default_backup_alert_threshold_pct: 80,
    move_frozen_source_hours: 48,
    move_cut_alert_seconds: 90,
    staging_autopurge_days: 90,
    audit_log_retention_days: 90,
    job_retention_days: 180,
    job_log_retention_days: 30,
    host_session_retention_days: 365,
    github_event_truncate_days: 7,
    github_event_retention_days: 90,
    odoo_log_archive_days: 60,
    container_log_max_size: "10m",
    container_log_max_file: 3,
    log_download_max_mb: 64,
    log_search_max_files: 60,
    log_search_timeout_s: 30,
    cron_heartbeat_url: "",
    trusted_proxy_ranges: "",
    metrics_enabled: false,
    metrics_central_url: "",
    metrics_remote_write_url: "",
    metrics_remote_write_token: "",
    metrics_retention_days: 90,
    metrics_collect_pg: true,
    metrics_central_public_host: "",
    metrics_central_direct_host: "",
    acme_dns_cloudflare_token: "",
    metrics_central_operator_sources: "",
    metrics_central_single_path: true,
    metrics_central_write_access: "servers",
};

/**
 * The form a part shows, from what /cloud/get_general_settings answered.
 *
 * Numbers that the server gives as 0 for "never set" keep their defaults
 * where 0 is not a value of theirs (log rotation and bounds, metrics
 * retention); retention days keep 0, which means "keep all".
 *
 * @param {string} part a key of CORE_PARTS
 * @param {object} data the answer
 * @returns {object} the form
 */
export function coreForm(part, data) {
    const zeroMeansUnset = new Set([
        "odoo_log_archive_days", "container_log_max_size", "container_log_max_file",
        "log_download_max_mb", "log_search_max_files", "log_search_timeout_s",
        "metrics_retention_days",
    ]);
    const form = {};
    for (const key of CORE_PARTS[part]) {
        let value = data[key];
        if (WRITE_ONLY.includes(key)) {
            value = "";
        } else if (typeof DEFAULTS[key] === "boolean") {
            value = Boolean(value);
        } else if (key === "default_backup_backend_id") {
            value = value || null;
        } else if (value === undefined || value === null || (zeroMeansUnset.has(key) && !value)) {
            value = DEFAULTS[key];
        }
        form[key] = value;
    }
    return form;
}

/**
 * What a part sends: the fields changed since it was loaded or last saved.
 *
 * Only those, so a value a job wrote meanwhile (the metrics URLs that
 * "Enable observability" fills in, say) is not put back by a stale form.
 * The default destination goes as 0 when there is none: nothing sent means
 * "unchanged".
 *
 * @param {object} changes the changed fields and their values
 * @returns {object} the values to send
 */
export function coreSaveVals(changes) {
    const vals = { ...changes };
    if ("default_backup_backend_id" in vals) {
        vals.default_backup_backend_id = vals.default_backup_backend_id || 0;
    }
    return vals;
}

/**
 * One section of the panel's own settings: General, Instances, Logs &
 * retention, Network edge or Monitoring.
 *
 * Loads what the section shows, saves only its fields, and joins the page's
 * save bar once loaded. A section that failed to load shows the error and
 * Retry, never a form of defaults Save would send.
 */
export class CoreSettingsBlock extends Component {
    static template = "incubacloud.CoreSettingsBlock";
    static components = {
        IcConfirmDialog, RlSelect, SettingAdvanced, SettingField, SettingRow, SettingsBlock,
    };
    static props = {
        part: { type: String, validate: (p) => p in CORE_PARTS },
    };

    /** Read the section's settings; join the save bar once mounted. */
    setup() {
        this.loaded = false;
        this.state = useState({
            loading: true,
            loadError: null,
            form: {},
            backupBackends: [],
            hosts: [],
            centralHostId: null,
            deployingCentral: false,
            // Where the central runs now ({id, name}), or null.
            centralHost: null,
            // Whether Retire the central is offered here.
            centralRetirable: false,
            // What the picked host offers the central, or null.
            centralSizing: null,
            retiringCentral: false,
            confirmDialog: null,
            auditPurging: false,
            // Read-only, derived on the server; never sent back.
            effectiveProxyRanges: [],
            trustedProxySource: "none",
            hasMetricsToken: false,
            hasAcmeDnsToken: false,
            centralDirect: null,
            // The central's single name: its check, or false without one.
            centralName: false,
            // Whether a module above decides who may write to the central.
            writeAccessFixed: false,
            checkingCentralName: false,
        });
        // The page's record of what is saved, for "Save first".
        this.formStore = this.env.settingsForm ? useState(this.env.settingsForm.store) : null;
        onWillStart(() => this.load());
        onMounted(() => this._register());
        onWillUnmount(() => this.env.unregisterSettingsSave?.(this.key));
    }

    /** @returns {string} the key the part saves under */
    get key() {
        return `Core:${this.props.part}`;
    }

    /** Read the part's settings, and what its pickers offer. */
    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const part = this.props.part;
            const [data, backends] = await Promise.all([
                rpc("/cloud/get_general_settings", {}),
                part === "general" ? rpc("/cloud/get_backup_backends", {}) : null,
            ]);
            this.state.form = coreForm(part, data);
            this.state.backupBackends = backends?.items || backends || [];
            this.state.effectiveProxyRanges = data.effective_trusted_proxy_ranges || [];
            this.state.trustedProxySource = data.trusted_proxy_source || "none";
            this.state.hasMetricsToken = Boolean(data.has_metrics_remote_write_token);
            this.state.hasAcmeDnsToken = Boolean(data.has_acme_dns_token);
            this.state.centralDirect = data.metrics_central_direct || null;
            this.state.centralName = data.metrics_central_name || false;
            this.state.writeAccessFixed = Boolean(data.metrics_central_write_access_fixed);
            this.state.centralHost = data.metrics_central_host || null;
            this.state.centralRetirable = Boolean(data.metrics_central_retirable);
            if (part === "monitoring") {
                // The central's destination picker. The rest of the page
                // works without it.
                try {
                    const hosts = await rpc("/cloud/get_hosts", {});
                    this.state.hosts = hosts.hosts || hosts || [];
                } catch {
                    this.state.hosts = [];
                }
            }
            this.loaded = true;
        } catch {
            this.state.loadError = _t("Failed to load these settings.");
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Join the page's save bar, once the form holds what the server has and
     * only while mounted (a retry can answer after the page moved on).
     */
    _register() {
        if (this.loaded && status(this) === "mounted") {
            this.env.registerSettingsSave?.(this.key, () => this._save(), this.state, WRITE_ONLY);
        }
    }

    /** Load again after a failure, and join the save bar if it worked. */
    async reload() {
        await this.load();
        this._register();
    }

    /**
     * Send the part's changed fields; a stored secret left empty is kept.
     *
     * @returns {Promise<object>} the server's answer
     */
    async _save() {
        const form = this.state.form;
        const vals = coreSaveVals(this.env.settingsForm?.changes(this.key, form) ?? { ...form });
        const result = await rpc("/cloud/save_general_settings", vals);
        if (result?.ok !== false) {
            if (vals.metrics_remote_write_token) {
                this.state.hasMetricsToken = true;
            }
            if (vals.acme_dns_cloudflare_token) {
                this.state.hasAcmeDnsToken = true;
            }
            for (const key of WRITE_ONLY) {
                if (key in this.state.form && this.state.form[key] === vals[key]) {
                    this.state.form[key] = "";
                }
            }
        }
        return result;
    }

    // ── General ───────────────────────────────────────────────────────

    /** @returns {{value: number, label: string}[]} the destinations */
    get backupBackendOptions() {
        return this.state.backupBackends.map((bb) => ({ value: bb.id, label: bb.name }));
    }

    // ── Logs ──────────────────────────────────────────────────────────

    /** @returns {boolean} whether Purge would use a retention not yet saved */
    get auditRetentionUnsaved() {
        // Read here so this block renders again when it changes.
        void this.state.form.audit_log_retention_days;
        return Boolean(this.env.settingsForm?.isChanged("audit_log_retention_days", this.formStore));
    }

    /** @returns {string} why an action waits for the save */
    get saveFirstText() {
        return _t("Save first: it uses the saved value.");
    }

    /** @returns {string} what the empty credential field says */
    get generatedText() {
        return _t("Generated on enable");
    }

    /** Purge the audit log entries older than the saved retention, now. */
    async purgeAuditLogs() {
        this.state.auditPurging = true;
        try {
            const result = await rpc("/cloud/purge_audit_logs", {
                days: this.state.form.audit_log_retention_days,
            });
            if (result.ok) {
                this.env.toast?.success(
                    result.deleted
                        ? _t("Deleted %s old audit log entries", result.deleted)
                        : _t("No old entries to purge"),
                );
            }
        } catch {
            this.env.toast?.error(_t("Failed to purge audit logs"));
        } finally {
            this.state.auditPurging = false;
        }
    }

    // ── Network edge ──────────────────────────────────────────────────

    /** @returns {string} where the applied ranges come from */
    get proxySourceLabel() {
        return {
            settings: _t("Source: the override above."),
            cloudflare: _t("Source: the platform's CDN, refreshed daily."),
        }[this.state.trustedProxySource] || "";
    }

    // ── Monitoring ────────────────────────────────────────────────────

    /** @returns {{value: number, label: string}[]} the hosts the central can run on */
    get centralHostOptions() {
        return this.state.hosts.map((h) => ({ value: h.id, label: h.name }));
    }

    /**
     * What the button under Where the central runs does with the picked
     * host: deploy the central there, or, while it runs on another host,
     * start an empty one there.
     *
     * @returns {"enable"|"empty"} the action
     */
    get centralAction() {
        return centralAction(this.state.centralHost, this.state.centralHostId);
    }

    /**
     * Pick the central's host and read whether the central fits on it.
     *
     * @param {number|null} hostId the picked host
     */
    async pickCentralHost(hostId) {
        this.state.centralHostId = hostId;
        this.state.centralSizing = null;
        if (!hostId) {
            return;
        }
        try {
            const sizing = await rpc("/cloud/monitoring/central_sizing", { host_id: hostId });
            // Only the answer for the host still picked.
            if (this.state.centralHostId === hostId && sizing?.ok) {
                this.state.centralSizing = sizing;
            }
        } catch {
            // The note is a help, not a condition: the picker works without it.
        }
    }

    /** @returns {string} what Start an empty central leaves behind */
    get centralElsewhereText() {
        return _t(
            "The central runs on %s, with the history. Here an empty one starts, and the history stays behind there.",
            this.state.centralHost?.name || "",
        );
    }

    /** @returns {string} what the picked host gives the central, in one line */
    get centralSizingSummary() {
        const sizing = this.state.centralSizing;
        if (!sizing?.measured) {
            return "";
        }
        if (sizing.fits) {
            return _t(
                "It fits: %(disk)s GB of disk free, %(ram)s GB of memory, %(cpu)s cores.",
                { disk: sizing.disk_free_gb, ram: sizing.ram_total_gb, cpu: sizing.cpu_cores },
            );
        }
        return "";
    }

    /** @returns {string} where observability stands, in one line */
    get observabilityStatus() {
        const form = this.state.form;
        if (!form.metrics_enabled) {
            return _t("Off. Pick a host above and enable it.");
        }
        if (!form.metrics_central_url) {
            return _t("On, but no query URL is set: nothing can be read back.");
        }
        if (this.state.centralHost) {
            return _t("Collecting, with charts available. The central runs on %s.", this.state.centralHost.name);
        }
        return _t("Collecting, with charts available.");
    }

    /** @returns {string} what the central's direct name is doing, or "" */
    get centralDirectStatus() {
        const status = this.state.centralDirect;
        if (!status || !this.state.form.metrics_central_direct_host) {
            return "";
        }
        if (status.state === "ok") {
            return _t("Working, certificate valid until %s", (status.cert_expires || "").slice(0, 10));
        }
        if (status.state === "failing") {
            return _t("Failing since %s", (status.failing_since || "").slice(0, 16));
        }
        return _t("Not checked yet");
    }

    /** @returns {{value: string, label: string}[]} who may write to the central */
    get writeAccessOptions() {
        return [
            { value: "servers", label: _t("Only this panel's servers, by their address") },
            { value: "anyone", label: _t("Any address, with each account's credential") },
        ];
    }

    /** @returns {string} what the central's single name answered, or "" */
    get centralNameStatus() {
        const status = this.state.centralName;
        if (!status) {
            return "";
        }
        if (status.state === "ok" && status.in_force) {
            return _t("The panel and the agents use %(url)s; certificate valid until %(date)s.", {
                url: status.url,
                date: (status.cert_expires || "").slice(0, 10),
            });
        }
        if (status.state === "failing" && status.in_force) {
            return _t("Not answering since %(since)s (%(error)s).", {
                since: (status.failing_since || "").slice(0, 16),
                error: status.error,
            });
        }
        if (status.record) {
            return _t("Waiting for the name: %s", status.record);
        }
        return _t("Not checked yet");
    }

    /** Check the central's single name now, from the central's server. */
    async checkCentralName() {
        if (this.state.checkingCentralName) {
            return;
        }
        this.state.checkingCentralName = true;
        try {
            const res = await rpc("/cloud/monitoring/check_central_name", {});
            if (res?.ok) {
                await announceJob(this.env.toast, res.job_id);
            } else {
                this.env.toast?.error(res?.error || _t("Could not check it."));
            }
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Could not check it."));
        } finally {
            this.state.checkingCentralName = false;
        }
    }

    /**
     * Queue the metrics central on the chosen host. A parameter, not a
     * fixed host: moving the central later is running this again.
     */
    async deployCentral() {
        if (this.state.deployingCentral) {
            return;
        }
        const hostId = this.state.centralHostId;
        if (!hostId) {
            this.env.toast?.error(_t("Pick the host that should run it."));
            return;
        }
        const empty = this.centralAction === "empty";
        if (empty) {
            const target = this.state.hosts.find((h) => h.id === hostId)?.name || "";
            let message = _t(
                "Start an empty central on %(target)s? The central on %(central)s is left as it is, with the metrics history and the security logs, and the panel no longer reads it: the charts start empty. %(central)s can then be deleted, and the history goes with it.",
                { target, central: this.state.centralHost.name },
            );
            const publicName = this.state.form.metrics_central_public_host;
            if (publicName) {
                // The panel and the agents reach the central by that name.
                message += " " + _t(
                    "Point %(name)s at %(target)s in DNS first: until it does, the panel and the agents still reach the central on %(central)s.",
                    { name: publicName, target, central: this.state.centralHost.name },
                );
            }
            const confirmed = await confirmVia(this.state, {
                title: _t("Start an empty central"),
                message,
                confirmLabel: _t("Start an empty central"),
                isDanger: true,
            });
            if (!confirmed) {
                return;
            }
        }
        this.state.deployingCentral = true;
        try {
            const res = empty
                ? await rpc("/cloud/monitoring/empty_central", { host_id: hostId })
                : await rpc("/cloud/monitoring/deploy_central", { host_id: hostId });
            if (res?.ok) {
                await announceJob(this.env.toast, res.job_id);
            } else {
                this.env.toast?.error(res?.error || _t("Could not queue it."));
            }
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Could not queue it."));
        } finally {
            this.state.deployingCentral = false;
        }
    }

    /**
     * Forget the central after the person confirmed: observability goes
     * off, and the server it ran on can be deleted.
     */
    async retireCentral() {
        const central = this.state.centralHost;
        if (!central || this.state.retiringCentral) {
            return;
        }
        const confirmed = await confirmVia(this.state, {
            title: _t("Retire the central"),
            message: _t(
                "Retire the central on %s? The panel forgets it and switches observability off: the metrics history and the security logs stay on that server, unread, and go when it is deleted. Agents already installed keep sending to it until observability is enabled on another host.",
                central.name,
            ),
            confirmLabel: _t("Retire the central"),
            isDanger: true,
        });
        if (!confirmed) {
            return;
        }
        this.state.retiringCentral = true;
        try {
            const res = await rpc("/cloud/monitoring/retire_central", {});
            if (res?.ok) {
                this.env.toast?.success(_t("The central is retired. Observability is off."));
                await this.reload();
            } else {
                this.env.toast?.error(res?.error || _t("Could not retire it."));
            }
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Could not retire it."));
        } finally {
            this.state.retiringCentral = false;
        }
    }
}

/**
 * What the button under Where the central runs does with a picked host.
 *
 * @param {{id: number, name: string}|null} central where the central runs now
 * @param {number|null} hostId the picked host
 * @returns {"enable"|"empty"} "empty" while the central runs on another host
 */
export function centralAction(central, hostId) {
    return central && hostId && central.id !== hostId ? "empty" : "enable";
}

/** Settings → Git providers: the GitLab servers, each saved on its own. */
export class GitProvidersBlock extends Component {
    static template = "incubacloud.GitProvidersBlock";
    static components = { GitProvidersTab };
    static props = {};
}
