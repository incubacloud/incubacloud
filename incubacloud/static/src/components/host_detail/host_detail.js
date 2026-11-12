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
import { IcMenu } from "../ic_menu/ic_menu";
import { agoText, parseUTC } from "../../utils/dates";
import { ACTIVE_STATES, announceJob, cancelPrompt, holdReason, jobStateText, jobTone } from "../../utils/job_queue";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";
import { useDebouncedBus, refreshesOn } from "../../utils/use_debounced_bus";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";
import { required, hostname, portRange, when } from "../../utils/validators";
import { confirmVia } from "../../utils/use_confirm";
import { HostHardeningModal } from "../host_hardening_modal/host_hardening_modal";
import { HostBoard } from "../metrics/host_board";

/** Tabs a link may open the page on (``?tab=``). */
const LINKABLE_TABS = new Set(["overview", "metrics"]);

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
    // "generate": the panel makes the key; "upload": the person's own file.
    key_source:               "generate",
    key_file:                 null,
    // The generated private key, sealed by the server: the browser cannot read it.
    generated_key:            "",
    description:              "",
    wildcard_domain:          "",
    traefik_panel_password:   "",
    traefik_config_yml:       "",
    traefik_inverseproxy_yaml: "",
    traefik_yml:              "",
    exclude_from_autoassign:  false,
    http_conn_rate:           50,
    trusted_proxy_ranges:     "",
    block_direct_access:      false,
    cdn_client_ca:            "",
    direct_port:              0,
    direct_path_opt_out:      false,
    tls_default_cert:         "",
    tls_default_key:          "",
    whitelist:                [...DEFAULT_WHITELIST],
    newWhitelistEntry:        "",
});

export class HostDetail extends Component {
    static props = {
        host_id: { type: Number, optional: true },
        tab: { type: String, optional: true },
    };
    static template = "incubacloud.HostDetail";
    static components = {
        PasswordInput, RemoteFileBrowser, TagSelector, IcConfirmDialog, RlSelect, IcPager,
        HostHardeningModal, IcMenu, HostBoard,
    };

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
            tab: this.isNew ? "general" : (LINKABLE_TABS.has(this.props.tab) ? this.props.tab : "overview"),
            // The job whose "Action required" says why, opened by a click.
            holdOpen: null,
            // Derived server-side and read-only: the ranges the host is
            // actually filtered against, where they come from, and what
            // its Traefik was last given. Kept out of `form` so they are
            // never sent back on save.
            effectiveProxyRanges: [],
            trustedProxySource: "none",
            trustedProxiesShipped: "",
            directPortShipped: 0,
            directAllowlist: [],
            directAllowlistPending: false,
            agentsWriteUrl: "",
            // Whether this panel may send security logs at all.
            securityLogsAvailable: true,
            // Whether the saved server has Send security logs on: it can
            // always be turned off.
            securityLogsSaved: false,
            directReachReason: "",
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
            // The default certificate's key is write-only: the server says
            // whether one is stored, and describes the certificate.
            has_tls_default_key:       false,
            tls_default_info:          null,
            // The public half of the key the panel generated for a new host,
            // and whether generating it failed.
            generatedPublicKey:        "",
            keyGenerateFailed:         false,
            // Which copy button just copied: "key", "command" or "".
            copied:                    "",
            // Whether the generated key was copied out of the page: from
            // then on leaving without creating the host would lose a key
            // that may already be on a server.
            keyShared:                 false,
            // Whether the panel's server has sshpass: without it hosts log
            // in with a key only (the server says so with the host's data).
            passwordLoginAvailable:    true,
            trusting_host_key:         false,
            // Code of the pluggable action being enqueued, "" when idle.
            actionBusy: "",
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
        // The hardening dialog; ``offer`` when the panel opened it for
        // the operator instead of the operator asking for it.
        this.hardeningState = useState({ open: false, offer: false });

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
                    (form) => this.isNew
                        && form.login_type === "ssh_key"
                        && form.key_source === "upload",
                    required(_t("SSH private key is required")),
                ),
            ],
            generated_key: [
                when(
                    (form) => this.isNew
                        && form.login_type === "ssh_key"
                        && form.key_source === "generate",
                    required(_t("No key has been generated. Try again, or upload your own.")),
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
            if (!refreshesOn(payload)) return;
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
        if (this.isNew && this.state.keyShared
            && this.state.form.key_source === "generate") return true;
        if (!this._savedForm) return false;
        return this._currentFormKey() !== this._savedForm;
    }

    _currentFormKey() {
        // Exclude transient UI state from the dirty baseline:
        //  - newWhitelistEntry: the uncommitted text in the whitelist
        //    input box. Typing there without hitting Add must not mark
        //    the form as dirty — the persisted value is ``whitelist``,
        //    not the half-typed hostname.
        //  - generated_key: the panel fills it in as the form opens.
        const { newWhitelistEntry, generated_key, ...persisted } = this.state.form;
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
            this.state.passwordLoginAvailable = defaults.password_login_available !== false;
        } catch (_e) { console.debug("Host defaults fetch skipped:", _e); }
        try {
            const tagsRes = await rpc("/cloud/get_tags", { scope: "host" });
            this.state.allTags = tagsRes.tags || [];
        } catch (_e) { console.debug("Host tag catalog fetch skipped:", _e); }
        this._snapshotForm();
        // Not awaited: the form opens without waiting for the key.
        this.generateKey();
    }

    /**
     * Ask the server for a new host's SSH key pair.
     *
     * Made as the form opens, before the host has an address, so the
     * public half can go into the provider's form while the server is
     * ordered. The private half arrives sealed and is only sent back
     * with the host.
     */
    async generateKey() {
        this.state.keyGenerateFailed = false;
        try {
            const pair = await rpc("/cloud/generate_host_key", {});
            this.state.form.generated_key = pair.sealed_key;
            this.state.generatedPublicKey = pair.public_key;
        } catch (_e) {
            this.state.keyGenerateFailed = true;
        }
    }

    async loadHost() {
        this.state.loading = true;
        try {
            const host = await rpc("/cloud/get_host", { host_id: this.props.host_id });
            this.state.host = host;
            this.state.passwordLoginAvailable     = host.password_login_available !== false;
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
                http_conn_rate:           host.http_conn_rate || 0,
                behind_cdn:               host.behind_cdn || false,
                trusted_proxy_ranges:     host.trusted_proxy_ranges || "",
                block_direct_access:      host.block_direct_access || false,
                cdn_client_ca:            host.cdn_client_ca || "",
                direct_port:              host.direct_port || 0,
                direct_path_opt_out:      host.direct_path_opt_out || false,
                // Saved hosts only: a new one starts with the panel's default.
                security_logs_enabled:    !!host.security_logs_enabled,
                tls_default_cert:         host.tls_default_cert || "",
                tls_default_key:          "",
                whitelist:                host.whitelist || [],
                newWhitelistEntry:        "",
            };
            this.state.has_tls_default_key = host.has_tls_default_key || false;
            this.state.tls_default_info = host.tls_default_info || null;
            // Read-only, derived server-side: what the host is actually
            // filtered against, versus the override box above it.
            this.state.effectiveProxyRanges =
                host.effective_trusted_proxy_ranges || [];
            this.state.trustedProxySource = host.trusted_proxy_source || "none";
            this.state.trustedProxiesShipped = host.trusted_proxies_shipped || "";
            // Read-only: what the direct port admits and what the agents use.
            this.state.directPortShipped = host.direct_port_shipped || 0;
            this.state.directAllowlist = host.direct_allowlist_shipped || [];
            this.state.directAllowlistPending = host.direct_allowlist_pending || false;
            this.state.agentsWriteUrl = host.metrics_agents_write_url || "";
            this.state.directReachReason = host.direct_reach_reason || "";
            this.state.securityLogsAvailable = host.security_logs_available !== false;
            this.state.securityLogsSaved = !!host.security_logs_enabled;
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

    /**
     * Whether this person sees the server's Metrics tab: whoever may
     * read metrics, on a saved host.
     */
    get canViewMetrics() {
        return !this.isNew && !!this.env.permissions?.can_view_metrics;
    }

    /**
     * True when this panel's own operator edits the observability
     * settings, so the board's stubs may point at Settings.
     *
     * @see Monitoring#canConfigure — same axis, same reason.
     */
    get canConfigureObservability() {
        return this.env.features?.observability?.configure !== false;
    }

    /**
     * Whether this panel collects metrics at all. Off, the agents already
     * installed on a server keep running until it is on again.
     */
    get observabilityCollects() {
        return this.env.features?.observability?.collect !== false;
    }

    /**
     * Whether this server's Send security logs may be changed here. On,
     * it can always be turned off; off, it can be turned on where the
     * panel collects metrics and may send security logs at all (a
     * managed panel waits for its manager's permission).
     */
    get securityLogsSwitchable() {
        if (this.state.securityLogsSaved) {
            return true;
        }
        return this.observabilityCollects && this.state.securityLogsAvailable !== false;
    }

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

    /**
     * The login types the form offers.
     *
     * Without sshpass on the panel's server, the key only: Ansible needs
     * sshpass to log in with a password. A saved host that already logs
     * in with one keeps that option, so its form still shows it.
     *
     * @returns {{value: string, label: string}[]}
     */
    get loginTypeOptions() {
        const options = [{ value: "ssh_key", label: _t("SSH Key") }];
        if (this.state.passwordLoginAvailable || this.state.host?.login_type === "password") {
            options.push({ value: "password", label: _t("Password") });
        }
        return options;
    }

    /**
     * Whether the form shows a host that logs in with a password on a
     * panel whose server has no sshpass.
     *
     * Ansible cannot work such a host (setup, hardening and probes fail),
     * so the form says so instead of leaving a bare authentication error.
     *
     * @returns {boolean}
     */
    get passwordHostWithoutSshpass() {
        return !this.state.passwordLoginAvailable && this.state.form.login_type === "password";
    }

    /**
     * The public key the Connection tab shows, or "".
     *
     * For a new host, the generated one; for a saved host, the half of
     * the stored key, until a new file is picked to replace it.
     */
    get sshPublicKey() {
        if (this.state.form.login_type !== "ssh_key") return "";
        if (this.isNew) {
            return this.state.form.key_source === "generate"
                ? this.state.generatedPublicKey
                : "";
        }
        if (this.state.form.key_file) return "";
        return this.state.host?.ssh_public_key || "";
    }

    /**
     * The command that adds the public key to a server's authorized keys.
     *
     * The key goes in single quotes, any quote inside it closed and
     * escaped, so no part of the line runs as a command.
     */
    get authorizedKeysCommand() {
        const quoted = `'${this.sshPublicKey.replaceAll("'", "'\\''")}'`;
        return `mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo ${quoted} >> ~/.ssh/authorized_keys`;
    }

    /**
     * Note that the generated key left the page (copied by a button or by
     * hand), so leaving a new host's form asks first. A saved host's key
     * is already stored: nothing to lose.
     */
    markKeyShared() {
        if (this.isNew) this.state.keyShared = true;
    }

    /**
     * Copy *text* to the clipboard and mark *which* button did it.
     *
     * @param {string} text what to copy
     * @param {string} which "key" or "command", for the button's check mark
     */
    async copyText(text, which) {
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            this.env.toast?.error(_t("Could not copy. Select the text and copy it."));
            return;
        }
        this.markKeyShared();
        this.state.copied = which;
        setTimeout(() => {
            if (this.state.copied === which) this.state.copied = "";
        }, 2000);
    }

    onKeyFileChange(ev) {
        const file = ev.target.files?.[0];
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
            const jobId = await this.orm.call(
                'cloud.job', 'enqueue', [this.props.host_id, false, 'host_probe'],
            );
            await announceJob(this.env.toast, jobId);
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to start host check."));
        }
    }

    /**
     * Open the hardening dialog.
     *
     * Refused while the form has unsaved changes: the hardening reads the
     * host's saved values, not the ones on screen.
     *
     * @param {boolean} [offer=false] opened on the operator's behalf
     */
    openHardeningModal(offer = false) {
        if (!this.state.host) return;
        if (this.hasUnsavedChanges) {
            this.env.toast?.warning(
                _t("Save your changes first: the hardening reads the saved values.")
            );
            return;
        }
        Object.assign(this.hardeningState, { open: true, offer });
    }

    /** Close the hardening dialog. */
    closeHardeningModal() {
        Object.assign(this.hardeningState, { open: false, offer: false });
    }

    /**
     * Refresh the host and its jobs once the hardening is queued.
     *
     * When the dialog added the asker's address to the SSH list, the
     * list as stored replaces the form's, and the saved baseline's too:
     * the page shows it without reading as unsaved, and a later save of
     * any other field writes it back as it is. Only where the form holds
     * the list (a layer's Security tab); the bus-driven refresh
     * never touches the form.
     *
     * @param {number[]} [jobIds] the jobs queued
     * @param {string} [allowedSshIps] the list as stored, when it changed
     */
    onHardeningApplied(jobIds, allowedSshIps) {
        if (typeof allowedSshIps === "string" && "allowed_ssh_ips" in this.state.form) {
            this.state.form.allowed_ssh_ips = allowedSshIps;
            if (this._savedForm) {
                const saved = JSON.parse(this._savedForm);
                saved.form.allowed_ssh_ips = allowedSshIps;
                this._savedForm = JSON.stringify(saved);
            }
        }
        this._silentRefresh();
    }

    async runFullSetup() {
        try {
            const jobId = await this.orm.call(
                'cloud.job', 'enqueue', [this.props.host_id, false, 'full_setup'],
            );
            await announceJob(this.env.toast, jobId);
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to start full setup."));
        }
    }

    /**
     * Describe where this host's effective proxy ranges come from.
     *
     * @returns {string} a phrase for the read-only line under the
     *   override box, or "" when nothing applies.
     */
    proxySourceLabel() {
        return {
            host: _t("this host's own override, below"),
            cloudflare: _t("the platform's CDN, refreshed daily"),
            settings: _t("the general settings"),
        }[this.state.trustedProxySource] || "";
    }

    /**
     * Whether the direct port typed differs from the one the proxy publishes.
     *
     * @returns {boolean} true when Full Setup still has to apply it.
     */
    get directPortPending() {
        const typed = parseInt(this.state.form.direct_port, 10) || 0;
        return (this.state.directPortShipped || 0) !== typed;
    }

    /**
     * Whether the host is running the proxy settings currently saved.
     *
     * @returns {boolean} true when a run is still needed for them to
     *   take effect.
     */
    proxyChangePending() {
        const shipped = (this.state.trustedProxiesShipped || "")
            .split("\n").map((r) => r.trim()).filter(Boolean).join(",");
        const applied = (this.state.effectiveProxyRanges || []).join(",");
        return shipped !== applied;
    }

    /**
     * Run one of the pluggable host actions.
     *
     * ``enqueue`` only creates the job and hands it to the worker, so
     * the RPC returns long before anything happens on the host. The
     * only thing the panel used to show was a new row down in Recent
     * Activity, 300 ms later via the bus — invisible to whoever is
     * looking at the button they just pressed, which is how a working
     * action came to look like a dead one. Hence the notice — started,
     * or queued behind the host's running job (``announceJob``) — and
     * the in-flight guard so a second press cannot double-queue.
     *
     * @param {{code: string, name: string, action_confirm?: string}} action
     *   The action as served in ``host.custom_actions``.
     */
    async hostAction(action) {
        if (this.state.actionBusy) return;
        // Actions that disrupt the host say so up front: this one
        // restarts the proxy this panel is served through, so the
        // "connection interrupted" that follows is expected, not a
        // failure to explain afterwards.
        if (action.action_confirm) {
            const confirmed = await this._confirm({
                title: action.name,
                message: action.action_confirm,
                confirmLabel: _t("Run"),
            });
            if (!confirmed) return;
        }
        this.state.actionBusy = action.code;
        try {
            const jobId = await this.orm.call(
                'cloud.job', 'enqueue', [this.props.host_id, false, action.code],
            );
            // Don't wait for the bus event for the row to show up: on a
            // job that restarts this host's proxy the bus connection is
            // the next casualty, and instance_detail already refreshes
            // eagerly here for the same reason.
            this._silentRefresh();
            await announceJob(this.env.toast, jobId);
        } catch (e) {
            this.env.toast?.error(e.data?.message || e.message || _t("Failed to enqueue host action."));
        } finally {
            this.state.actionBusy = "";
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
                    // A key trusted for the first time is a server that
                    // has just been connected: the moment to offer the
                    // hardening, which is otherwise easy to never run.
                    if (this.state.host?.hardened === false) {
                        this.openHardeningModal(true);
                    }
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

    /**
     * Open an interactive login shell on this host in a new tab.
     *
     * Asks first, in red: it is root on the whole machine, not a shell
     * inside one container. The notice says exactly what is audited —
     * who, when and from where — and that the session content is not,
     * because a shell receives passwords on stdin.
     */
    async openHostShell() {
        const host = this.state.host;
        if (!host?.id) return;

        const ok = await this._confirm({
            title: _t("Open host shell"),
            message: _t(
                'You are about to open a shell directly on host "%s" (%s) ' +
                'as user %s. Anything you run affects the whole server. ' +
                'Who opens it, when and from where is recorded; what you ' +
                'type is not. Continue?',
                host.name || host.ip_address,
                host.ip_address || "?",
                host.user || "?",
            ),
            confirmLabel: _t("Open Host Shell"),
            isDanger: true,
        });
        if (!ok) return;

        let res;
        try {
            res = await rpc("/cloud/host_terminal/open", { host_id: host.id });
        } catch {
            this.env.toast?.error(_t("Failed to open host shell"));
            return;
        }
        if (!res?.ok) {
            this.env.toast?.error(res?.error || _t("Failed to open host shell"));
            return;
        }
        window.open(`/cloud/host_terminal/${res.session_id}`, "_blank", "noopener");
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
            // Only the key the person chose goes to the server.
            if (vals.key_source === "generate") delete vals.key_file;
            else delete vals.generated_key;
            delete vals.key_source;
            vals.whitelist = Array.from(this.state.form.whitelist || []);
            if (vals.port) vals.port = parseInt(vals.port, 10);
            vals.http_conn_rate = parseInt(vals.http_conn_rate, 10) || 0;
            vals.direct_port = parseInt(vals.direct_port, 10) || 0;
            vals.tag_ids = this.state.selectedTags.map(t => t.id);

            if (this.isNew) {
                const result = await rpc("/cloud/create_host", { vals });
                this.env.refreshNavCounts?.();
                // Clear the dirty baseline before navigating so the
                // guard doesn't fire on the programmatic transition.
                this.state.keyShared = false;
                this._snapshotForm();
                this.env.navigate("host_detail", { host_id: result.id });
            } else {
                await rpc("/cloud/save_host", { host_id: this.props.host_id, vals });
                this.state.host.name = vals.name;
                await this._refreshDefaultCertificate();
                this._snapshotForm();
                this.env.toast?.success(_t("Changes saved"));
            }
        } catch (e) {
            // A refused certificate says why (half a pair, a key from
            // another certificate); a generic message would hide that.
            const reason = e?.data?.message;
            this.env.toast?.error(
                reason || (this.isNew ? _t("Failed to create host.") : _t("Failed to save host."))
            );
        } finally {
            this.state.saving = false;
        }
    }

    /**
     * Re-read what the server now holds for the default certificate and
     * the SSH key.
     *
     * After a save the pasted keys must leave the form (they are never
     * shown again), the description must follow the certificate just
     * stored and the public key the SSH key just uploaded.
     */
    async _refreshDefaultCertificate() {
        const host = await rpc("/cloud/get_host", { host_id: this.props.host_id });
        this.state.form.tls_default_key = "";
        this.state.form.key_file = null;
        this.state.host.ssh_public_key = host.ssh_public_key || "";
        this.state.has_key_file = host.has_key_file || false;
        this.state.has_tls_default_key = host.has_tls_default_key || false;
        this.state.tls_default_info = host.tls_default_info || null;
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
            const res = await rpc("/cloud/setup_whitelist", { host_id: this.props.host_id });
            await announceJob(this.env.toast, res?.job_id);
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
                // A deployed host is torn down by a job; one where
                // nothing was deployed is gone already and answers none.
                await announceJob(this.env.toast, result.job_id);
                this.env.refreshNavCounts?.();
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
     * Tooltip listing what the saved Traefik config is missing.
     *
     * The specifics matter here: "behind the templates" alone would send
     * the operator diffing three files by hand, which is how this drift
     * went unnoticed in the first place.
     *
     * @returns {string} the pill's ``title`` text
     */
    get templateDriftTitle() {
        const details = this.state.host.template_drift_details || "";
        return (
            "The saved Traefik configuration is missing settings the " +
            "shipped templates declare. Upgrading the module adds them; " +
            "re-run full setup afterwards to ship them.\n\n" + details
        );
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

    /**
   * Human-readable observability status for this host.
   *
   * Enrolment converges on its own, so the useful thing to show is
   * whether metrics are actually arriving — not whether somebody
   * remembered to press something.
   */
  get observabilityLabel() {
    const host = this.state.host || {};
    const state = host.metrics_agents_state || "never";
    if (state === "installed") {
      return host.metrics_last_seen
        ? _t("Reporting")
        : _t("Agents installed, no data yet");
    }
    if (state === "failed") {
      return _t("Install failed — retrying automatically");
    }
    return _t("Not enrolled yet");
  }

  hasMetrics() {
        const h = this.state.host;
        return h && (h.cpu_cores > 0 || h.ram_total_gb > 0 || h.disk > 0);
    }

    formatRam(gb) {
        if (!gb) return '—';
        return gb % 1 === 0 ? `${gb} GB` : `${gb.toFixed(1)} GB`;
    }

    /**
     * A number of CPU cores, as the host's overview reads it.
     *
     * @param {number} n the cores.
     * @returns {string} "4 cores"
     */
    coresLabel(n) {
        return _t("%s cores", n);
    }

    diskExtra() {
        const h = this.state.host;
        const free = h.disk_free_gb;
        const pct = h.disk;
        if (!free && !pct) return '';
        if (!pct || pct <= 0 || pct >= 100) return _t("%s GB free", free);
        const total = Math.round(free * 100 / (100 - pct));
        return _t("%(free)s GB free / %(total)s GB", { free, total });
    }

    formatDate(dateStr) {
        if (!dateStr) return "";
        const d = parseUTC(dateStr);
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    timeAgo(dateStr) {
        if (!dateStr) return "";
        return agoText(Math.floor((Date.now() - parseUTC(dateStr).getTime()) / 1000));
    }

    isActiveJob(state) {
        return ACTIVE_STATES.includes(state);
    }

    /**
     * Text of a job's state pill (``job_queue.jobStateText``).
     *
     * @param {{state: string, queue_position?: number}} job
     * @returns {string}
     */
    jobStateText(job) {
        return jobStateText(job);
    }

    /**
     * Colour of a job's state in the recent activity (``job_queue.jobTone``).
     *
     * @param {string} state
     * @param {boolean} [stopping] cancelled and still stopping
     * @returns {string}
     */
    jobTone(state, stopping = false) {
        return jobTone(state, stopping);
    }

    /**
     * Why a job reads "Action required" (``job_queue.holdReason``).
     *
     * @param {object} job a ``_format`` row
     * @returns {string}
     */
    holdReason(job) {
        return holdReason(job);
    }

    /**
     * Open or close the reason of *job*'s "Action required".
     *
     * @param {{id: number}} job
     */
    toggleHold(job) {
        this.state.holdOpen = this.state.holdOpen === job.id ? null : job.id;
    }

    /**
     * Close the reason on Escape, and give the focus back to its chip.
     *
     * @param {KeyboardEvent} ev
     */
    onHoldKeydown(ev) {
        if (ev.key !== "Escape" || this.state.holdOpen === null) return;
        ev.stopPropagation();
        this.state.holdOpen = null;
        ev.currentTarget.querySelector(".ic-tl-alert-badge")?.focus();
    }

    /**
     * Open the alert that keeps *job*, in the alerts panel.
     *
     * @param {{action_alert_id?: number|null}} job
     */
    openHoldAlert(job) {
        this.state.holdOpen = null;
        this.env.openAlert(job.action_alert_id || false);
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
            queued: "jst-queued", blocked: "jst-blocked",
        }[state] || "jst-pending";
    }

    openLog(job) {
        window.open(`/cloud/log/${job.id}`, "_blank");
    }

    async cancelJob(ev, job) {
        ev.stopPropagation();
        const ok = await this._confirm({
            title: _t("Cancel job"),
            message: cancelPrompt(job),
            confirmLabel: _t("Cancel job"),
            isDanger: true,
        });
        if (ok) {
            try {
                await this.orm.call("cloud.job", "cancel_job", [job.id]);
            } catch (e) {
                this.env.toast?.error(_t("Cancel failed: ") + (e.data?.message || e.message));
            }
            this._loadJobs();
        }
    }

    async retryJob(ev, job) {
        ev.stopPropagation();
        try {
            const newJobId = await this.orm.call("cloud.job", "retry_job", [job.id]);
            if (newJobId) await announceJob(this.env.toast, newJobId);
        } catch (e) {
            this.env.toast?.error(_t("Retry failed: ") + (e.data?.message || e.message));
        }
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
