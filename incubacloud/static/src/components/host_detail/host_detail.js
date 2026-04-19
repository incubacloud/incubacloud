import { Component, useState, onWillStart, onMounted, onWillUnmount, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { PasswordInput } from "../password_input/password_input";
import { RemoteFileBrowser } from "../remote_file_browser/remote_file_browser";
import { TagSelector } from "../tag_selector/tag_selector";
import { parseUTC } from "../../utils/dates";

const TRAEFIK_FILES = [
    { key: "traefik_config_yml",        label: "config.yml" },
    { key: "traefik_inverseproxy_yaml", label: "inverseproxy.yaml" },
    { key: "traefik_yml",               label: "traefik.yml" },
];

const DEFAULT_WHITELIST = [
    "cdnjs.cloudflare.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "www.google.com",
    "www.gravatar.com",
];

const EMPTY_FORM = () => ({
    name:                     "",
    type:                     "self_hosted",
    ip_address:               "",
    port:                     22,
    user:                     "",
    login_type:               "ssh_key",
    password:                 "",
    key_file:                 null,
    description:              "",
    wildcard_domain:          "",
    traefik_panel_password:   "",
    traefik_config_yml:       "",
    traefik_inverseproxy_yaml: "",
    traefik_yml:              "",
    exclude_from_autoassign:  false,
    whitelist:                [...DEFAULT_WHITELIST],
    newWhitelistEntry:        "",
});

export class HostDetail extends Component {
    static props = { host_id: { type: Number, optional: true } };
    static template = "incubacloud.HostDetail";
    static components = { PasswordInput, RemoteFileBrowser, TagSelector };

    setup() {
        this.env = useEnv();
        this.orm = useService("orm");
        this.traefikFiles = TRAEFIK_FILES;
        this.isNew = !this.props.host_id;

        this.state = useState({
            tab: this.isNew ? "general" : "overview",
            traefik_file: "traefik_config_yml",
            loading: !this.isNew,
            saving: false,
            deleting: false,
            confirmDialog: null,
            error: null,
            host: this.isNew ? { name: _t("New Host"), status: "unknown", instance_count: 0 } : null,
            form: this.isNew ? EMPTY_FORM() : {},
            has_password:              false,
            has_key_file:              false,
            has_known_hosts_key:       false,
            has_traefik_panel_password: false,
            trusting_host_key:         false,
            // Tags
            selectedTags: [],
            allTags: [],
            // Jobs
            jobs: [],
            jobsTotal: 0,
            jobsLoading: false,
            // Import instance modal
            showImportBrowser: false,
            // Audit log
            auditLog: [],
            auditLogLoading: false,
            auditFilter: { q: '', action: '', date_from: '', date_to: '' },
            auditActions: [],
            auditPurging: false,
        });

        if (this.isNew) {
            onWillStart(() => this._loadDefaults());
        } else {
            onWillStart(() => this.loadHost());
        }

        this._busService = useService("bus_service");
        this._onJobUpdate = async (payload) => {
            if (this._destroyed) return;
            try {
                const [job] = await this.orm.call("cloud.job", "load_jobs", [payload.id]);
                if (this._destroyed) return;
                if (job && job.host_id === this.props.host_id) {
                    this._silentRefresh();
                }
            } catch (_e) { console.debug("Bus handler skipped (component destroyed):", _e); }
        };

        onMounted(() => {
            if (!this.isNew) {
                this._pollTimer = setInterval(() => this._silentRefresh(), 8000);
                this._busService.subscribe("cloud_jobs", this._onJobUpdate);
                this._busService.start();
            }
        });
        onWillUnmount(() => {
            this._destroyed = true;
            clearInterval(this._pollTimer);
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
        });
    }

    // ── Data loading ─────────────────────────────────────────────────────

    async _loadDefaults() {
        try {
            const defaults = await rpc("/cloud/host_defaults", {});
            Object.assign(this.state.form, {
                traefik_config_yml: defaults.traefik_config_yml || "",
                traefik_inverseproxy_yaml: defaults.traefik_inverseproxy_yaml || "",
                traefik_yml: defaults.traefik_yml || "",
            });
        } catch (_e) { console.debug("Host defaults fetch skipped:", _e); }
        try {
            const tagsRes = await rpc("/cloud/get_tags", { scope: "host" });
            this.state.allTags = tagsRes.tags || [];
        } catch (_e) { console.debug("Host tag catalog fetch skipped:", _e); }
    }

    async loadHost() {
        this.state.loading = true;
        try {
            const host = await rpc("/cloud/get_host", { host_id: this.props.host_id });
            this.state.host = host;
            this.state.has_password               = host.has_password || false;
            this.state.has_key_file               = host.has_key_file || false;
            this.state.has_known_hosts_key        = host.has_known_hosts_key || false;
            this.state.has_traefik_panel_password  = host.has_traefik_panel_password || false;
            this.state.form = {
                name:                     host.name || "",
                type:                     host.type || "self_hosted",
                ip_address:               host.ip_address || "",
                port:                     host.port || 22,
                user:                     host.user || "",
                login_type:               host.login_type || "ssh_key",
                password:                 "",
                key_file:                 null,
                description:              host.description || "",
                wildcard_domain:          host.wildcard_domain || "",
                traefik_panel_password:   "",
                traefik_config_yml:       host.traefik_config_yml || "",
                traefik_inverseproxy_yaml: host.traefik_inverseproxy_yaml || "",
                traefik_yml:              host.traefik_yml || "",
                exclude_from_autoassign:  host.exclude_from_autoassign || false,
                whitelist:                host.whitelist || [],
                newWhitelistEntry:        "",
            };
            this.state.selectedTags = [...(host.tags || [])];
            this.state.allTags = [...(host.all_tags || [])];
            this._loadJobs();
        } catch (e) {
            this.state.error = _t("Failed to load host.");
        } finally {
            this.state.loading = false;
        }
    }

    async _loadJobs() {
        this.state.jobsLoading = true;
        try {
            const data = await this.orm.call("cloud.job", "get_host_jobs", [
                this.props.host_id, 5, 0,
            ]);
            this.state.jobs = data.jobs;
            this.state.jobsTotal = data.total;
        } catch (_e) { console.warn("Failed to load jobs:", _e); }
        this.state.jobsLoading = false;
    }

    async _silentRefresh() {
        try {
            const host = await rpc("/cloud/get_host", { host_id: this.props.host_id });
            this.state.host = host;
            const data = await this.orm.call("cloud.job", "get_host_jobs", [
                this.props.host_id, 5, 0,
            ]);
            this.state.jobs = data.jobs;
            this.state.jobsTotal = data.total;
        } catch (_e) { console.debug("Silent refresh failed:", _e); }
    }

    get hasMoreJobs() {
        return this.state.jobsTotal > this.state.jobs.length;
    }

    viewAllJobs() {
        this.env.toggleSlideOver("jobs", null, this.props.host_id);
    }

    // ── Tabs ─────────────────────────────────────────────────────────────

    setTab(tab) {
        this.state.tab = tab;
        if (tab === 'activity' && !this.state.auditLog.length && !this.state.auditLogLoading) {
            this._loadAuditLog();
        }
    }

    async _loadAuditLog() {
        this.state.auditLogLoading = true;
        const f = this.state.auditFilter;
        try {
            const entries = await rpc("/cloud/get_audit_log", {
                host_id: this.props.host_id,
                q: f.q || null,
                action_filter: f.action || null,
                date_from: f.date_from || null,
                date_to: f.date_to || null,
            });
            this.state.auditLog = entries || [];
            if (!f.q && !f.action && !f.date_from && !f.date_to) {
                const seen = new Set();
                this.state.auditActions = (entries || [])
                    .map(e => e.action)
                    .filter(a => a && !seen.has(a) && seen.add(a));
            }
        } catch (_e) { console.warn("Failed to load audit log:", _e); }
        this.state.auditLogLoading = false;
    }

    applyAuditFilter() { this._loadAuditLog(); }

    resetAuditFilter() {
        this.state.auditFilter = { q: '', action: '', date_from: '', date_to: '' };
        this._loadAuditLog();
    }

    async purgeAuditLogs() {
        this.state.auditPurging = true;
        try {
            const result = await rpc("/cloud/purge_audit_logs", {});
            if (result.ok) {
                this.env.toast?.success(
                    result.deleted
                        ? _t("Deleted %s old audit log entries").replace('%s', result.deleted)
                        : _t("No old entries to purge")
                );
                this._loadAuditLog();
            }
        } catch (_) {
            this.env.toast?.error(_t("Failed to purge audit logs"));
        }
        this.state.auditPurging = false;
    }

    setTraefikFile(key) { this.state.traefik_file = key; }

    // ── Form handlers ────────────────────────────────────────────────────

    onInput(field, ev) {
        this.state.form[field] = ev.target.value;

    }

    onSelect(field, ev) {
        this.state.form[field] = ev.target.value;

    }

    onPasswordChange(field, value) {
        this.state.form[field] = value;

    }

    onKeyFileChange(ev) {
        const file = ev.target.files[0];
        if (!file) {
            this.state.form.key_file = null;
            return;
        }
        const reader = new FileReader();
        reader.onload = (e) => { this.state.form.key_file = btoa(e.target.result); };
        reader.readAsText(file);
    }

    // ── Import instance ─────────────────────────────────────────────────

    openImportBrowser() {
        this.state.showImportBrowser = true;
    }

    closeImportBrowser() {
        this.state.showImportBrowser = false;
    }

    onImportSelect(result) {
        this.state.showImportBrowser = false;
        this.env.navigate("instance_detail", {
            project_id: result.project_id,
            instance_id: result.instance_id,
        });
    }

    // ── Actions ──────────────────────────────────────────────────────────

    async runCheck() {
        try {
            await this.orm.call('cloud.job', 'enqueue', [this.props.host_id, false, 'host_probe']);
        } catch {
            this.env.toast?.error(_t("Failed to start host check."));
        }
    }

    async runFullSetup() {
        try {
            await this.orm.call('cloud.job', 'enqueue', [this.props.host_id, false, 'full_setup']);
        } catch {
            this.env.toast?.error(_t("Failed to start full setup."));
        }
    }

    async hostAction(code) {
        try {
            await this.orm.call('cloud.job', 'enqueue', [this.props.host_id, false, code]);
        } catch {
            this.env.toast?.error(_t("Failed to enqueue host action."));
        }
    }

    async trustHostKey() {
        if (this.state.trusting_host_key) return;
        this.state.trusting_host_key = true;
        try {
            const res = await rpc("/cloud/trust_host_key", { host_id: this.props.host_id });
            if (res?.ok) {
                this.state.has_known_hosts_key = true;
                this.state.host = { ...this.state.host, has_known_hosts_key: true };
                this.env.toast?.success(_t("SSH host key trusted successfully."));
            } else {
                this.env.toast?.error(res?.error || _t("Failed to trust SSH host key."));
            }
        } catch {
            this.env.toast?.error(_t("Failed to trust SSH host key."));
        } finally {
            this.state.trusting_host_key = false;
        }
    }

    _validate() {
        const f = this.state.form;
        const missing = [];
        if (!f.name?.trim()) missing.push(_t("Name"));
        if (!f.ip_address?.trim()) missing.push(_t("IP Address"));
        if (!f.port) missing.push(_t("Port"));
        if (!f.user?.trim()) missing.push(_t("User"));
        if (!f.login_type) missing.push(_t("Login Type"));
        if (f.login_type === 'password' && this.isNew && !f.password && !this.state.has_password) {
            missing.push(_t("Password"));
        }
        if (f.login_type === 'ssh_key' && this.isNew && !f.key_file) {
            missing.push(_t("SSH Private Key"));
        }
        if (!f.wildcard_domain?.trim()) missing.push(_t("Wildcard Domain"));
        // traefik_panel_password is auto-generated when empty — no validation needed
        if (!f.traefik_config_yml?.trim()) missing.push("config.yml");
        if (!f.traefik_inverseproxy_yaml?.trim()) missing.push("inverseproxy.yaml");
        if (!f.traefik_yml?.trim()) missing.push("traefik.yml");
        return missing;
    }

    // ── Tags ─────────────────────────────────────────────────────────────

    addTag(tag) {
        if (!this.state.selectedTags.find(t => t.id === tag.id)) {
            this.state.selectedTags.push(tag);
        }
    }

    removeTag(tagId) {
        const idx = this.state.selectedTags.findIndex(t => t.id === tagId);
        if (idx !== -1) this.state.selectedTags.splice(idx, 1);
    }

    async createTag(name) {
        const tag = await rpc("/cloud/create_tag", { name, scope: "host" });
        if (!this.state.allTags.find(t => t.id === tag.id)) {
            this.state.allTags.push(tag);
        }
        return tag;
    }

    async save() {
        if (this.state.saving) return;
        const missing = this._validate();
        if (missing.length) {
            this.env.toast?.error(_t("Required fields missing: %s").replace('%s', missing.join(", ")));
            return;
        }
        this.state.saving = true;
        try {
            const vals = { ...this.state.form };
            delete vals.newWhitelistEntry;
            vals.whitelist = Array.from(this.state.form.whitelist || []);
            if (vals.port) vals.port = parseInt(vals.port, 10);
            vals.tag_ids = this.state.selectedTags.map(t => t.id);

            if (this.isNew) {
                const result = await rpc("/cloud/create_host", { vals });
                this.env.navigate("host_detail", { host_id: result.id });
            } else {
                await rpc("/cloud/save_host", { host_id: this.props.host_id, vals });
                this.state.host.name = vals.name;
                this.env.toast?.success(_t("Changes saved"));
            }
        } catch (e) {
            this.env.toast?.error(this.isNew ? _t("Failed to create host.") : _t("Failed to save host."));
        } finally {
            this.state.saving = false;
        }
    }

    async addWhitelistEntry() {
        const entry = (this.state.form.newWhitelistEntry || "").trim().toLowerCase();
        if (!entry || this.state.form.whitelist.includes(entry)) return;
        this.state.form.whitelist = [...this.state.form.whitelist, entry];
        this.state.form.newWhitelistEntry = "";
        await this.save();
    }

    async removeWhitelistEntry(hostname) {
        this.state.form.whitelist = this.state.form.whitelist.filter(h => h !== hostname);
        await this.save();
    }

    onWhitelistKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.addWhitelistEntry();
        }
    }

    async applyWhitelist() {
        if (!this.props.host_id) return;
        await this.save();
        try {
            await rpc("/cloud/setup_whitelist", { host_id: this.props.host_id });
        } catch (e) {
            this.env.toast?.error(_t("Failed to enqueue whitelist job."));
        }
    }

    goBack() { this.env.navigate("hosts"); }

    _confirm({ title, message, confirmLabel = _t("Confirm"), isDanger = false }) {
        return new Promise((resolve) => {
            this.state.confirmDialog = {
                title, message, confirmLabel, isDanger,
                onConfirm: () => { this.state.confirmDialog = null; resolve(true); },
                onCancel:  () => { this.state.confirmDialog = null; resolve(false); },
            };
        });
    }

    async deleteHost() {
        const hasRemote = this.state.host.traefik_deployed;
        const confirmed = await this._confirm({
            title: _t("Delete Host"),
            message: hasRemote
                ? _t("Remove \"%s\"? Traefik will be stopped and all remote files deleted.").replace('%s', this.state.host.name)
                : _t("Remove \"%s\"? Nothing has been deployed so no remote cleanup is needed.").replace('%s', this.state.host.name),
            confirmLabel: _t("Delete"),
            isDanger: true,
        });
        if (!confirmed) return;
        this.state.deleting = true;
        try {
            const result = await rpc("/cloud/delete_host", { host_id: this.props.host_id });
            if (result.error) {
                this.env.toast?.error(result.error);
            } else {
                this.env.navigate("hosts");
            }
        } catch (e) {
            this.env.toast?.error(_t("Failed to delete host."));
        } finally {
            this.state.deleting = false;
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────

    statusLabel(status) {
        return {
            compatible:  _t("Compatible"),
            degraded:    _t("Degraded"),
            unsupported: _t("Unsupported"),
            checking:    _t("Checking"),
            unknown:     _t("Unknown"),
        }[status] || status;
    }

    barClass(value) {
        if (!value) return 'bar-empty';
        if (value >= 85) return 'bar-danger';
        if (value >= 65) return 'bar-warn';
        return 'bar-ok';
    }

    hasMetrics() {
        const h = this.state.host;
        return h && (h.cpu_cores > 0 || h.ram_total_gb > 0 || h.disk > 0);
    }

    formatRam(gb) {
        if (!gb) return '—';
        return gb % 1 === 0 ? `${gb} GB` : `${gb.toFixed(1)} GB`;
    }

    diskExtra() {
        const h = this.state.host;
        const free = h.disk_free_gb;
        const pct = h.disk;
        if (!free && !pct) return '';
        if (!pct || pct <= 0 || pct >= 100) return `${free} GB free`;
        const total = Math.round(free * 100 / (100 - pct));
        return `${free} GB free / ${total} GB`;
    }

    formatDate(dateStr) {
        if (!dateStr) return "";
        const d = parseUTC(dateStr);
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    timeAgo(dateStr) {
        if (!dateStr) return "";
        const secs = Math.floor((Date.now() - parseUTC(dateStr).getTime()) / 1000);
        if (secs < 60) return _t("just now");
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        return `${Math.floor(hrs / 24)}d ago`;
    }

    isActiveJob(state) {
        return ["pending", "enqueued", "started", "wait_dependencies"].includes(state);
    }

    jobIcon(code) {
        return {
            host_probe: "fa-heartbeat",
            full_setup: "fa-wrench",
            deploy_instance: "fa-rocket",
            rebuild_instance: "fa-wrench",
            setup_whitelist: "fa-shield",
            docker_prune: "fa-recycle",
            host_metrics: "fa-bar-chart",
        }[code] || "fa-cog";
    }

    jobStateClass(state) {
        return {
            done: "jst-done", failed: "jst-failed", cancelled: "jst-cancelled",
            started: "jst-running", pending: "jst-pending", enqueued: "jst-pending",
            blocked: "jst-blocked",
        }[state] || "jst-pending";
    }

    openLog(job) {
        window.open(`/cloud/log/${job.id}`, "_blank");
    }

    async cancelJob(ev, job) {
        ev.stopPropagation();
        const ok = await this._confirm({
            title: _t("Cancel job"),
            message: _t("Cancel \"%s\"? This cannot be undone.").replace('%s', job.name),
            confirmLabel: _t("Cancel job"),
            isDanger: true,
        });
        if (ok) {
            await this.orm.call("cloud.job", "cancel_job", [job.id]);
            this._loadJobs();
        }
    }

    async retryJob(ev, job) {
        ev.stopPropagation();
        const newJobId = await this.orm.call("cloud.job", "retry_job", [job.id]);
        if (newJobId) window.open(`/cloud/log/${newJobId}`, "_blank");
        this._loadJobs();
    }

    getLineNumbers(field) {
        const lines = Math.max(1, (this.state.form[field] || "").split("\n").length);
        return Array.from({ length: lines }, (_, i) => i + 1);
    }

    onCodeScroll(ev) {
        const gutter = ev.target.previousElementSibling;
        if (gutter && gutter.classList.contains("ic-code-gutter")) {
            gutter.scrollTop = ev.target.scrollTop;
        }
    }
}
