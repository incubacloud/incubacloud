import { Component, useState, onWillStart, onMounted, onWillUnmount, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { PasswordInput } from "../password_input/password_input";
import { RemoteFileBrowser } from "../remote_file_browser/remote_file_browser";
import { TagSelector } from "../tag_selector/tag_selector";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { RlSelect } from "../rl_select/rl_select";
import { IcPager } from "../ic_pager/ic_pager";
import { parseUTC } from "../../utils/dates";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";
import { useDebouncedBus } from "../../utils/use_debounced_bus";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";
import { required, hostname, portRange, when } from "../../utils/validators";
import { confirmVia } from "../../utils/use_confirm";

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
    static components = { PasswordInput, RemoteFileBrowser, TagSelector, IcConfirmDialog, RlSelect, IcPager };

    /** Rows per page for the server-paginated audit log. */
    get pageSize() {
        return 20;
    }

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
            auditOffset: 0,
            auditTotal: 0,
            auditFilter: { q: '', action: '', date_from: '', date_to: '' },
            auditActions: [],
            auditPurging: false,
        });

        // Dirty tracking: snapshot the form after every load/save so
        // ``hasUnsavedChanges`` reflects real divergence, not the
        // initial load flicker. Selected tags are tracked separately
        // in the snapshot because they live outside ``state.form``.
        this._savedForm = null;

        this.validator = useFormValidation(() => ({
            name: [required(_t("Host name is required"))],
            ip_address: [
                required(_t("IP address or hostname is required")),
                hostname(),
            ],
            port: [required(_t("Port is required")), portRange()],
            user: [required(_t("SSH user is required"))],
            login_type: [required(_t("Login method is required"))],
            wildcard_domain: [required(_t("Wildcard domain is required"))],
            traefik_config_yml: [required(_t("config.yml is required"))],
            traefik_inverseproxy_yaml: [required(_t("inverseproxy.yaml is required"))],
            traefik_yml: [required(_t("traefik.yml is required"))],
            password: [
                when(
                    (form) => this.isNew
                        && form.login_type === "password"
                        && !this.state.has_password,
                    required(_t("Password is required for password login")),
                ),
            ],
            key_file: [
                when(
                    (form) => this.isNew && form.login_type === "ssh_key",
                    required(_t("SSH private key is required")),
                ),
            ],
        }));

        if (this.isNew) {
            onWillStart(() => this._loadDefaults());
        } else {
            onWillStart(() => this.loadHost());
        }

        this._busService = useService("bus_service");
        const triggerRefresh = useDebouncedBus(() => {
            if (!this._destroyed) this._silentRefresh();
        });
        // Only this host's jobs. The filter was dropped once because
        // checking ``host_id`` meant a second RPC per event
        // (``load_jobs`` just to read the target); the bus payload now
        // carries it, so it is free again.
        //
        // It runs here rather than inside the debounced callback on
        // purpose: ``useDebouncedBus`` is last-write-wins on its
        // argument, so branching downstream would drop a matching
        // event that shared a window with a non-matching one.
        this._onJobUpdate = (payload) => {
            if (payload?.host_id !== this.props.host_id) return;
            triggerRefresh(payload.id);
        };

        onMounted(() => {
            if (!this.isNew) {
                this._busService.subscribe("cloud_jobs", this._onJobUpdate);
                this._busService.start();
            }
        });
        onWillUnmount(() => {
            this._destroyed = true;
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
        });
        // Bus covers the happy path; ``visibilitychange`` refresh catches
        // the rare case where the longpoll dropped an event while the tab
        // was backgrounded.
        useVisibilityRefresh(() => {
            if (!this.isNew && !this._destroyed) this._silentRefresh();
        });

        useNavGuard(
            () => this.hasUnsavedChanges,
            (opts) => this._confirm(opts),
        );
    }

    get hasUnsavedChanges() {
        if (!this._savedForm) return false;
        return this._currentFormKey() !== this._savedForm;
    }

    _currentFormKey() {
        // Exclude transient UI state from the dirty baseline:
        //  - newWhitelistEntry: the uncommitted text in the whitelist
        //    input box. Typing there without hitting Add must not mark
        //    the form as dirty — the persisted value is ``whitelist``,
        //    not the half-typed hostname.
        const { newWhitelistEntry, ...persisted } = this.state.form;
        return JSON.stringify({
            form: persisted,
            tags: (this.state.selectedTags || []).map(t => t.id).sort(),
        });
    }

    _snapshotForm() {
        this._savedForm = this._currentFormKey();
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
        this._snapshotForm();
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
            this._snapshotForm();
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
            const res = await rpc("/cloud/get_audit_log", {
                host_id: this.props.host_id,
                offset: this.state.auditOffset,
                limit: this.pageSize,
                q: f.q || null,
                action_filter: f.action || null,
                date_from: f.date_from || null,
                date_to: f.date_to || null,
            });
            this.state.auditLog = res.entries || [];
            this.state.auditTotal = res.total || 0;
            this.state.auditActions = res.actions || [];
        } catch (_e) { console.warn("Failed to load audit log:", _e); }
        this.state.auditLogLoading = false;
    }

    applyAuditFilter() { this.state.auditOffset = 0; this._loadAuditLog(); }

    /** Audit-log paginator: jump to a page and reload. */
    auditGoPage(offset) {
        this.state.auditOffset = offset;
        this._loadAuditLog();
    }

    resetAuditFilter() {
        this.state.auditFilter = { q: '', action: '', date_from: '', date_to: '' };
        this.state.auditOffset = 0;
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
                this.state.auditOffset = 0;
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
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to start host check."));
        }
    }

    async runFullSetup() {
        try {
            await this.orm.call('cloud.job', 'enqueue', [this.props.host_id, false, 'full_setup']);
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to start full setup."));
        }
    }

    async hostAction(code) {
        try {
            await this.orm.call('cloud.job', 'enqueue', [this.props.host_id, false, code]);
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to enqueue host action."));
        }
    }

    async trustHostKey() {
        if (this.state.trusting_host_key) return;
        this.state.trusting_host_key = true;
        try {
            const res = await rpc("/cloud/trust_host_key", { host_id: this.props.host_id });
            if (res?.ok) {
                this.state.has_known_hosts_key = true;
                this.state.host = {
                    ...this.state.host,
                    has_known_hosts_key: true,
                    known_hosts_fingerprint: res.fingerprint || "",
                    revoked_key_fingerprint: "",
                };
                // Whether this key matches the one revoked by the last
                // endpoint change is the machine-identity check; saying only
                // "trusted" would hide the answer the operator came for.
                if (res.changed) {
                    this.env.toast?.error(
                        _t("Key trusted, but it DIFFERS from the previously trusted one (%(old)s → %(new)s). Confirm this machine was rebuilt or replaced — see the alert.", { old: res.previous_fingerprint, new: res.fingerprint })
                    );
                } else if (res.previous_fingerprint) {
                    this.env.toast?.success(
                        _t("SSH host key trusted: %s — unchanged, same machine as before.", res.fingerprint)
                    );
                } else {
                    this.env.toast?.success(
                        _t("SSH host key trusted: %s", res.fingerprint || _t("captured"))
                    );
                }
            } else {
                this.env.toast?.error(res?.error || _t("Failed to trust SSH host key."));
            }
        } catch {
            this.env.toast?.error(_t("Failed to trust SSH host key."));
        } finally {
            this.state.trusting_host_key = false;
        }
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
        const { isValid, firstError } = this.validator.validate(this.state.form);
        if (!isValid) {
            this.env.toast?.error(firstError);
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
                // Clear the dirty baseline before navigating so the
                // guard doesn't fire on the programmatic transition.
                this._snapshotForm();
                this.env.navigate("host_detail", { host_id: result.id });
            } else {
                await rpc("/cloud/save_host", { host_id: this.props.host_id, vals });
                this.state.host.name = vals.name;
                this._snapshotForm();
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
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to enqueue whitelist job."));
        }
    }

    goBack() { this.env.navigate("hosts"); }

    /** Open the shared confirmation dialog. See utils/use_confirm.js. */
    _confirm(opts) {
        return confirmVia(this.state, opts);
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

    /**
     * Map a host status to a Relay state-badge (``rl-sb``) modifier class
     * for the hero badge. ``compatible``/``degraded`` map to themselves
     * (relay.scss defines both); the remaining statuses fall back to the
     * generic severity classes that share the badge styling.
     *
     * @param {string} status host lifecycle status
     * @returns {string} the ``rl-sb`` modifier class name
     */
    hostSbClass(status) {
        return {
            compatible:  "compatible",
            degraded:    "degraded",
            unsupported: "failed",
            checking:    "provisioning",
            unknown:     "inactive",
        }[status] || "inactive";
    }

    /**
     * Map a host status to a Relay overview-value (``rl-v``) modifier class
     * for the Overview "State" row, so the value text picks up the matching
     * severity colour.
     *
     * @param {string} status host lifecycle status
     * @returns {string} the ``rl-v`` modifier class name ('' for neutral)
     */
    hostVClass(status) {
        return {
            compatible:  "ok",
            degraded:    "warn",
            unsupported: "crit",
            checking:    "muted",
            unknown:     "muted",
        }[status] || "";
    }

    barClass(value) {
        if (!value) return 'bar-empty';
        if (value >= 85) return 'bar-danger';
        if (value >= 70) return 'bar-warn';
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
