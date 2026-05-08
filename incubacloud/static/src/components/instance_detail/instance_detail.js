import { Component, useState, onWillStart, onMounted, onWillUnmount, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { RepoEditor } from "../repo_editor/repo_editor";
import { parseUTC } from "../../utils/dates";
import { useSafePoll } from "../../utils/use_safe_poll";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";
import { useDebouncedBus } from "../../utils/use_debounced_bus";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";
import { required, instanceName, portRange, when } from "../../utils/validators";
import { PasswordInput } from "../password_input/password_input";
import { TagSelector, tagStyle, tagDotStyle } from "../tag_selector/tag_selector";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { IcModal } from "../ic_modal/ic_modal";

const ODOO_VERSIONS = [
    "7.0", "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
];
const PG_VERSIONS = ["14", "15", "16", "17", "18"];

// ── Pip requirements helpers ─────────────────────────────────────────────────

function _parseReqLine(line) {
    const stripped = line.split('#')[0].trim();
    if (!stripped) return null;
    const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
    if (!m) return null;
    const name = m[1].toLowerCase();
    const markerM = stripped.match(/;(.+)/);
    const marker = markerM ? markerM[1].trim().toLowerCase() : '';
    return { name, raw: stripped, key: marker ? `${name}|${marker}` : name };
}

function _formatSource(url, branch) {
    const m = (url || '').match(/github\.com[:/](.+)/);
    const name = m ? m[1].replace(/\.git$/, '') : (url || '').replace(/\/$/, '');
    return branch ? `${name} (${branch})` : name;
}

function _escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function _buildPkgMap(text) {
    const pkgMap = {};
    const re = /<<<<<<< (?<eSrc>[^\n]*)\n(?<eLine>[^\n]*)\n=======\n(?<iLine>[^\n]*)\n>>>>>>> (?<iSrc>[^\n]*)/g;
    let m;
    while ((m = re.exec(text || '')) !== null) {
        const parsed = _parseReqLine(m.groups.eLine.trim());
        if (parsed) {
            pkgMap[parsed.key] = {
                type: 'conflict', spec: parsed.raw, name: parsed.name,
                existingSrc: m.groups.eSrc.trim(), block: m[0],
            };
        }
    }
    const cleanOnly = (text || '').replace(
        /<<<<<<< [^\n]*\n[^\n]*\n=======\n[^\n]*\n>>>>>>> [^\n]*/g, ''
    );
    for (const line of cleanOnly.split('\n')) {
        const parsed = _parseReqLine(line);
        if (parsed && !(parsed.key in pkgMap)) {
            pkgMap[parsed.key] = { type: 'clean', spec: parsed.raw, name: parsed.name, block: null };
        }
    }
    return pkgMap;
}

/**
 * Merge incomingText into existingText with git-style conflict markers.
 * Returns { content, conflicts: [{name, existing, existingSource, incoming, incomingSource}] }
 */
function mergeRequirements(existingText, incomingText, repoUrl, repoBranch) {
    const sourceLabel = _formatSource(repoUrl, repoBranch);
    const pkgMap = _buildPkgMap(existingText);
    let newText = existingText || '';
    const added = [];
    const conflicts = [];

    for (const line of (incomingText || '').split('\n')) {
        const parsed = _parseReqLine(line);
        if (!parsed) continue;
        const { name, raw, key } = parsed;

        if (!(key in pkgMap)) {
            added.push(raw);
            pkgMap[key] = { type: 'clean', spec: raw, name, block: null };
        } else if (pkgMap[key].spec === raw) {
            // exact duplicate — skip
        } else if (pkgMap[key].type === 'conflict') {
            const oldBlock = pkgMap[key].block;
            const existingSrc = pkgMap[key].existingSrc;
            const existingSpec = pkgMap[key].spec;
            const newBlock = `<<<<<<< ${existingSrc}\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
            newText = newText.replace(oldBlock, newBlock);
            pkgMap[key].block = newBlock;
            conflicts.push({ name, existing: existingSpec, existingSource: existingSrc, incoming: raw, incomingSource: sourceLabel });
        } else {
            const existingSpec = pkgMap[key].spec;
            const newBlock = `<<<<<<< existing\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
            // Function form of replace: bypasses the ``$&`` / ``$1`` /
            // ``$$`` substitution rules that would otherwise corrupt
            // the merged text when ``newBlock`` contains a repo URL or
            // branch name with literal ``$`` characters.
            newText = newText.replace(
                new RegExp('^' + _escapeRegex(existingSpec) + '$', 'm'),
                () => newBlock,
            );
            pkgMap[key] = { type: 'conflict', spec: existingSpec, name, existingSrc: 'existing', block: newBlock };
            conflicts.push({ name, existing: existingSpec, existingSource: 'existing', incoming: raw, incomingSource: sourceLabel });
        }
    }

    if (added.length) {
        const base = newText.trimEnd();
        newText = base + (base ? '\n' : '') + added.join('\n');
    }

    return { content: newText, conflicts };
}

/**
 * Apply user-chosen resolutions to pip_dependencies text.
 * choices: { [packageName]: chosenSpec }
 */
function applyPipResolutions(text, choices) {
    return (text || '').replace(
        /<<<<<<< [^\n]*\n([^\n]*)\n=======\n[^\n]*\n>>>>>>> [^\n]*/g,
        (block, existingLine) => {
            const parsed = _parseReqLine(existingLine.trim());
            return (parsed && parsed.name in choices) ? choices[parsed.name] : block;
        }
    );
}

export class InstanceDetail extends Component {
    static props = {
        instance_id: { type: Number, optional: true },
        project_id:  { type: Number },
        environment: { type: String, optional: true },
        embedded:    { type: Boolean, optional: true },
    };
    static template = "incubacloud.InstanceDetail";
    static components = { RepoEditor, PasswordInput, TagSelector, IcConfirmDialog, IcModal };

    get isCreate() {
        return !this.props.instance_id;
    }

    get hasUnsavedChanges() {
        return this._savedForm && JSON.stringify(this.state.form) !== this._savedForm;
    }

    setup() {
        this.env = useEnv();
        this.orm = useService("orm");
        this.odooVersions = ODOO_VERSIONS;
        this.pgVersions   = PG_VERSIONS;

        this.state = useState({
            tab:      "overview",
            loading:  true,
            saving:   false,
            error:    null,
            hosts:    [],
            autoassignEnabled: false,
            backupBackends: [],
            langs:    [],
            langsLoading: false,
            _initialLangId: null,
            inst:     null,
            form:     {},
            selectedTags: [],
            allTags:  [],
            langSearch: "",
            langOpen:   false,
            repoSearch: "",
            pipDeps:          "",
            aptDeps:          "",
            pipConflictModal: null,  // { conflicts, choices }
            // has_* flags — whether a password is currently stored server-side
            has_odoo_admin_password:  false,
            has_postgres_password:    false,
            has_smtp_relay_password:  false,
            // sync modal
            syncModal:        null,  // { diff, actions, loading, applying, error }
            // action-related state
            actionLoading:    false,
            confirmDialog:    null,
            conflictBlocked:  null,
            restoreDialog:    null,
            connectDialog:    null,
            shellDialog:      null,
            // activity (jobs)
            jobs:             [],
            jobsTotal:        0,
            jobsLoading:      false,
            // backups
            backupsLoading:   false,
            backupsData:      null,
            backupsError:     null,
            backupsJobId:     null,
            downloadModal:    null,  // { time, type, loading }
            restoreBackupModal: null,  // { time, confirmName, loading }
            // audit log
            auditLog:         [],
            auditLogLoading:  false,
            auditFilter:      { q: '', action: '', date_from: '', date_to: '' },
            auditActions:     [],
            auditPurging:     false,
        });

        this._pollTimer = null;
        this._savedForm = null;

        this.validator = useFormValidation(() => ({
            name: [
                required(_t("Instance name is required")),
                instanceName(),
            ],
            host_id: [
                when(
                    () => !this.state.autoassignEnabled,
                    required(_t("Host is required when auto-assign is disabled")),
                ),
            ],
            smtp_relay_port: [portRange()],
        }));
        this._busService = useService("bus_service");
        // Shared guard for every recursive setTimeout(poll, …) in this
        // component. Registers onWillUnmount so zombie ticks can't
        // write into a destroyed component's state.
        this._safePoll = useSafePoll();

        // Debounced refresh of THIS instance's deeper detail
        // (jobs list, repos, etc. via /cloud/get_instance). The
        // project-level refresh — including sibling instances' status
        // dots — is owned by ``env.projectStore``, which subscribes
        // to the same bus once for the whole app. We do not call
        // ``refreshSidebar`` here: the store reacts to the same event
        // independently and the explicit hop would only duplicate the
        // /cloud/get_project_full RPC.
        const triggerRefresh = useDebouncedBus(async (jobId) => {
            if (this._destroyed || jobId == null) return;
            try {
                const [job] = await this.orm.call("cloud.job", "load_jobs", [jobId]);
                if (this._destroyed) return;
                if (job && job.instance_id === this.props.instance_id) {
                    this._silentRefresh();
                }
            } catch (_e) { /* component torn down mid-flight */ }
        });
        this._onJobUpdate = (payload) => triggerRefresh(payload.id);

        onWillStart(() => this.load());
        onMounted(() => {
            if (!this.isCreate) {
                this._busService.subscribe("cloud_jobs", this._onJobUpdate);
            }
        });
        onWillUnmount(() => {
            this._destroyed = true;
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
        });
        // Nav guard owns the ``beforeunload`` listener now — the old
        // direct listener was superseded to avoid duplicate handlers.
        useNavGuard(
            () => this.hasUnsavedChanges,
            (opts) => this._confirm(opts),
        );
        // Bus covers the happy path; ``visibilitychange`` refresh catches
        // events dropped while the tab was backgrounded.
        useVisibilityRefresh(() => {
            if (!this.isCreate && !this._destroyed) this._silentRefresh();
        });
    }

    async _silentRefresh() {
        try {
            const inst = await rpc("/cloud/get_instance", {
                instance_id: this.props.instance_id,
            });
            this.state.inst = inst;
            this.state.jobs = inst.jobs || [];
            this.state.jobsTotal = inst.jobsTotal || 0;
            // Patch the project store so the sidebar reflects this
            // instance's fresh status without waiting for its own bus
            // refresh tick.
            this.env.projectStore?.updateInstance(this.props.instance_id, inst);
        } catch (_e) { console.debug("Silent refresh failed:", _e); }
    }

    timeAgo(dateStr) {
        if (!dateStr) return "";
        const now = new Date();
        const d = parseUTC(dateStr);
        const secs = Math.floor((now - d) / 1000);
        if (secs < 60) return "just now";
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        const days = Math.floor(hrs / 24);
        return `${days}d ago`;
    }

    formatDate(dateStr) {
        if (!dateStr) return "";
        const d = parseUTC(dateStr);
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    isActiveJob(state) {
        return ["pending", "enqueued", "started", "wait_dependencies"].includes(state);
    }

    jobIcon(code) {
        const map = {
            deploy_instance: "fa-rocket",
            rebuild_instance: "fa-wrench",
            restart_instance: "fa-refresh",
            stop_instance: "fa-stop",
            start_instance: "fa-play",
            export_instance: "fa-download",
            delete_instance: "fa-trash-o",
            restore_instance: "fa-database",
        };
        return map[code] || "fa-cog";
    }

    jobStateClass(state) {
        return {
            done: "jst-done", failed: "jst-failed", cancelled: "jst-cancelled",
            started: "jst-running", pending: "jst-pending", enqueued: "jst-pending",
            blocked: "jst-blocked",
        }[state] || "jst-pending";
    }

    openJobLog(job) {
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
            if (newJobId) window.open(`/cloud/log/${newJobId}`, "_blank");
        } catch (e) {
            this.env.toast?.error(_t("Retry failed: ") + (e.data?.message || e.message));
        }
        this._loadJobs();
    }

    goResolveAlert(ev) {
        ev.stopPropagation();
        this.env.toggleSlideOver("alerts");
    }

    _applyInstanceData(inst, hosts, backends) {
        this.state.hosts = (hosts || []).filter(h => h.status === "compatible" && h.traefik_deployed);
        this.state.backupBackends = backends || [];
        this.state.inst = inst;
        this.state.jobs = inst.jobs || [];
        this.state.jobsTotal = inst.jobsTotal || 0;
        if (inst.odoo_initial_lang) {
            this.state._initialLangId = inst.odoo_initial_lang;
            this.state.langSearch = "(loading…)";
        }
        this.state.has_odoo_admin_password = inst.has_odoo_admin_password || false;
        this.state.has_postgres_password = inst.has_postgres_password || false;
        this.state.has_smtp_relay_password = inst.has_smtp_relay_password || false;
        this.state.pipDeps = inst.pip_dependencies || "";
        this.state.aptDeps = inst.apt_dependencies || "";
        this.state.form = {
            name:                inst.name || "",
            environment:         inst.environment || "staging",
            host_id:             inst.host_id || null,
            odoo_version:        inst.odoo_version || 19.0,
            odoo_initial_lang:   inst.odoo_initial_lang || null,
            odoo_admin_password: "",
            odoo_proxy:          inst.odoo_proxy || "traefik",
            postgres_version:    inst.postgres_version || "17",
            postgres_dbname:     inst.postgres_dbname || "",
            postgres_username:   inst.postgres_username || "",
            postgres_password:   "",
            domains:             (inst.domains || []).map(d => ({ ...d })),
            smtp_relay_host:     inst.smtp_relay_host || "",
            smtp_relay_port:     inst.smtp_relay_port || 587,
            smtp_relay_security: inst.smtp_relay_security || "starttls",
            smtp_relay_user:     inst.smtp_relay_user || "",
            smtp_relay_password: "",
            odoo_conf:           inst.odoo_conf || "",
            odoo_memory_limit:   inst.odoo_memory_limit || "2g",
            odoo_cpus:           inst.odoo_cpus || 2.0,
            db_memory_limit:     inst.db_memory_limit || "1g",
            db_cpus:             inst.db_cpus || 1.0,
            backup_memory_limit: inst.backup_memory_limit || "512m",
            backup_cpus:         inst.backup_cpus || 0.5,
            smtp_memory_limit:   inst.smtp_memory_limit || "256m",
            smtp_cpus:           inst.smtp_cpus || 0.25,
            backup_backend_id:   inst.backup_backend_id || null,
            auto_rebuild:        inst.auto_rebuild || false,
            auto_update:         inst.auto_update !== false,
            repos: (inst.repos || []).map(r => ({ ...r })),
        };
        this.state.selectedTags = [...(inst.tags || [])];
        this.state.allTags = [...(inst.all_tags || [])];
        this.state.backupsData = null;
        this.state.backupsError = null;
        this._savedForm = JSON.stringify(this.state.form);
    }

    async load() {
        this.state.loading = true;
        try {
            if (this.isCreate) {
                this.state.tab = "general";
                const [hostsResp, project, backends] = await Promise.all([
                    rpc("/cloud/get_hosts"),
                    rpc("/cloud/get_project", { project_id: this.props.project_id }),
                    rpc("/cloud/get_backup_backends", {}),
                ]);
                this.state.backupBackends = backends?.items || backends || [];
                const allHosts = hostsResp.hosts || hostsResp || [];
                this.state.autoassignEnabled = !!hostsResp.autoassign_enabled;
                this.state.hosts = allHosts.filter(h => h.status === "compatible" && h.traefik_deployed);
                const repos = (project?.repos || []).map(r => ({
                    id: null,
                    url: r.url || "",
                    branch: r.branch || "main",
                    addons: "",
                    excludes: "",
                    commit_sha: r.commit_sha || "",
                }));
                this.state.pipDeps = project?.pip_dependencies || "";
                this.state.aptDeps = project?.apt_dependencies || "";
                try {
                    const tagsRes = await rpc("/cloud/get_tags", { scope: "instance" });
                    this.state.allTags = tagsRes.tags || [];
                } catch (_e) { /* noop */ }
                this.state.form = {
                    name:                "",
                    environment:         this.props.environment || "staging",
                    host_id:             null,
                    odoo_version:        parseFloat(project?.odoo_version) || 19.0,
                    odoo_initial_lang:   null,
                    odoo_admin_password: "",
                    odoo_proxy:          "traefik",
                    postgres_version:    "17",
                    postgres_username:   "odoo",
                    postgres_password:   "",
                    domains:             [],
                    smtp_relay_host:     "",
                    smtp_relay_port:     587,
                    smtp_relay_security: "starttls",
                    smtp_relay_user:     "",
                    smtp_relay_password: "",
                    odoo_conf:           "",
                    odoo_memory_limit:   "2g",
                    odoo_cpus:           2.0,
                    db_memory_limit:     "1g",
                    db_cpus:             1.0,
                    backup_memory_limit: "512m",
                    backup_cpus:         0.5,
                    smtp_memory_limit:   "256m",
                    smtp_cpus:           0.25,
                    backup_backend_id:   project?.backup_backend_id || null,
                    auto_rebuild:        false,
                    auto_update:         true,
                    repos,
                };
                this._savedForm = JSON.stringify(this.state.form);
            } else {
                // Try the project store first — instant switch when
                // navigating between instances of the same project.
                const storeState = this.env.projectStore?.state;
                if (
                    storeState?.projectId === this.props.project_id
                    && storeState.data
                ) {
                    const cached = storeState.data.instances[this.props.instance_id];
                    if (cached) {
                        this._applyInstanceData(
                            cached,
                            storeState.data.hosts,
                            storeState.data.backup_backends,
                        );
                        this.state.loading = false;
                        return;
                    }
                }
                // Store miss — fall back to server.
                const inst = await rpc("/cloud/get_instance", {
                    instance_id: this.props.instance_id,
                });
                this._applyInstanceData(inst, this.state.hosts, this.state.backupBackends);
            }
        } catch (e) {
            this.state.error = this.isCreate ? _t("Failed to load project data.") : _t("Failed to load instance.");
        } finally {
            this.state.loading = false;
        }
    }

    async _loadJobs() {
        this.state.jobsLoading = true;
        try {
            const data = await this.orm.call("cloud.job", "get_instance_jobs", [
                this.props.instance_id, 5, 0,
            ]);
            this.state.jobs = data.jobs;
            this.state.jobsTotal = data.total;
        } catch (_e) { console.warn("Failed to load jobs:", _e); }
        this.state.jobsLoading = false;
    }

    get hasMoreJobs() {
        return this.state.jobsTotal > this.state.jobs.length;
    }

    viewAllJobs() {
        this.env.toggleSlideOver("jobs", this.props.instance_id);
    }

    setTab(tab) {
        this.state.tab = tab;
        if (tab === 'backups' && !this.state.backupsData && !this.state.backupsLoading) {
            this._loadBackups();
        }
        if (tab === 'activity' && !this.state.auditLog.length && !this.state.auditLogLoading) {
            this._loadAuditLog();
        }
    }

    onInput(field, ev) {
        this.state.form[field] = ev.target.value;

    }

    onDepsInput(ev) {
        this.state.pipDeps = ev.target.value;

    }

    onAptInput(ev) {
        this.state.aptDeps = ev.target.value;

    }

    onPasswordChange(field, value) {
        this.state.form[field] = value;

    }

    // ── Pip conflict modal ───────────────────────────────────────────────────

    openPipConflictModal(conflicts) {
        const choices = {};
        for (const c of conflicts) {
            choices[c.name] = c.existing;
        }
        this.state.pipConflictModal = { conflicts, choices };
    }

    closePipConflictModal() { this.state.pipConflictModal = null; }

    setPipChoice(name, spec) {
        if (this.state.pipConflictModal) {
            this.state.pipConflictModal.choices[name] = spec;
        }
    }

    confirmPipResolve() {
        const { choices } = this.state.pipConflictModal;
        this.state.pipConflictModal = null;
        this.state.pipDeps = applyPipResolutions(this.state.pipDeps, choices);

    }

    onHostSelect(ev) {
        const id = parseInt(ev.target.value);
        this.state.form.host_id = id || null;

    }

    onVersionSelect(ev) {
        this.state.form.odoo_version = parseFloat(ev.target.value);

    }

    onPgSelect(ev) {
        this.state.form.postgres_version = ev.target.value;

    }

    onEnvSelect(ev) {
        this.state.form.environment = ev.target.value;

    }

    // ── Domain management ──────────────────────────────────────────────

    addDomain() {
        this.state.form.domains.push({
            id: null, hostname: '', redirect_to: '', cert_resolver: 'letsencrypt',
        });

    }

    removeDomain(idx) {
        this.state.form.domains.splice(idx, 1);

    }

    onDomainChange(idx, field, value) {
        this.state.form.domains[idx][field] = value;

    }

    onBackupBackendSelect(ev) {
        const val = ev.target.value;
        this.state.form.backup_backend_id = val ? parseInt(val) : null;

    }

    async _ensureLangs() {
        if (this.state.langs.length) return;
        this.state.langsLoading = true;
        try {
            const langs = await rpc("/cloud/get_langs");
            this.state.langs = langs;
            if (this.state._initialLangId) {
                const found = langs.find(l => l.id === this.state._initialLangId);
                this.state.langSearch = found ? `${found.name} (${found.code})` : "";
                this.state._initialLangId = null;
            }
        } finally {
            this.state.langsLoading = false;
        }
    }

    get filteredLangs() {
        const q = this.state.langSearch.toLowerCase();
        const list = q
            ? this.state.langs.filter(l =>
                l.name.toLowerCase().includes(q) || l.code.toLowerCase().includes(q))
            : this.state.langs;
        return list.slice(0, 10);
    }

    get filteredRepos() {
        const q = (this.state.repoSearch || '').toLowerCase();
        const repos = this.state.form.repos || [];
        if (!q) return repos.map((r, i) => ({ ...r, _origIdx: i }));
        return repos
            .map((r, i) => ({ ...r, _origIdx: i }))
            .filter(r => (r.url || '').toLowerCase().includes(q));
    }

    onLangBlur() {
        setTimeout(() => { this.state.langOpen = false; }, 150);
    }

    onLangSearchInput(ev) {
        this.state.langSearch = ev.target.value;
        this.state.langOpen = true;
        if (!ev.target.value) {
            this.state.form.odoo_initial_lang = null;
        }

    }

    selectLang(lang) {
        if (lang) {
            this.state.form.odoo_initial_lang = lang.id;
            this.state.langSearch = `${lang.name} (${lang.code})`;
        } else {
            this.state.form.odoo_initial_lang = null;
            this.state.langSearch = "";
        }
        this.state.langOpen = false;

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
        const tag = await rpc("/cloud/create_tag", { name, scope: "instance" });
        if (!this.state.allTags.find(t => t.id === tag.id)) {
            this.state.allTags.push(tag);
        }
        return tag;
    }

    tagBadgeStyle(tag) { return tagStyle(tag); }
    tagDotStyle(tag) { return tagDotStyle(tag); }

    async save() {
        if (this.state.saving) return;
        const { isValid, firstError } = this.validator.validate(this.state.form);
        if (!isValid) {
            this.env.toast?.error(firstError);
            return;
        }
        this.state.saving = true;
        try {
            const vals = {
                ...this.state.form,
                smtp_relay_port: parseInt(this.state.form.smtp_relay_port) || 587,
                odoo_cpus: parseFloat(this.state.form.odoo_cpus) || 0,
                db_cpus: parseFloat(this.state.form.db_cpus) || 0,
                backup_cpus: parseFloat(this.state.form.backup_cpus) || 0,
                smtp_cpus: parseFloat(this.state.form.smtp_cpus) || 0,
                pip_dependencies: this.state.pipDeps,
                apt_dependencies: this.state.aptDeps,
                tag_ids: this.state.selectedTags.map(t => t.id),
            };
            if (this.isCreate) {
                vals.project_id      = this.props.project_id;
                vals.odoo_proxy      = "traefik";
                vals.postgres_dbname = "prod";
                const result = await rpc("/cloud/create_instance", { vals });
                if (result && result.blocked) {
                    this.env.toast?.error(result.message || _t("Cannot create instance: project has unresolved conflicts."));
                    return;
                }
                // Clear the dirty baseline before navigating so the nav
                // guard doesn't fire on the programmatic transition from
                // the create route to the new instance's detail route.
                this._savedForm = JSON.stringify(this.state.form);
                // Force-reload the store so the new instance shows up
                // in the sidebar at the destination route.
                await this.env.projectStore?.load(this.props.project_id);
                this.env.navigate("instance_detail", {
                    project_id: this.props.project_id,
                    instance_id: result.id,
                });
            } else {
                await rpc("/cloud/save_instance", {
                    instance_id: this.props.instance_id,
                    vals,
                });
                this.state.inst.name = vals.name;
                this.env.toast?.success(_t("Changes saved"));
                this._savedForm = JSON.stringify(this.state.form);
            }
        } catch (e) {
            this.env.toast?.error(this.isCreate
                ? ((e.data?.message || e.message) || _t("Failed to create instance."))
                : _t("Failed to save instance."));
        } finally {
            this.state.saving = false;
        }
    }

    // ── Repo management ───────────────────────────────────────────────────

    addRepo() {
        this.state.form.repos.unshift({ id: null, url: "", branch: "", addons: "", excludes: "", commit_sha: "" });

    }

    removeRepo(idx) {
        this.state.form.repos.splice(idx, 1);

    }

    onRepoChange(idx, field, value) {
        this.state.form.repos[idx][field] = value;

        if (field === 'branch' && value && this.state.form.repos[idx].url) {
            this._fetchAndMergeRequirements(this.state.form.repos[idx].url, value);
        }
    }

    async freezeAllRepos() {
        const ids = this.state.form.repos
            .filter(r => r.id && r.url && r.branch && !r.commit_sha)
            .map(r => r.id);
        if (!ids.length) return;
        this.state.freezingAll = true;
        try {
            const res = await rpc("/cloud/freeze_all_repos", {
                repo_ids: ids, model: "cloud.instance.repo",
            });
            if (res.ok) {
                for (const repo of this.state.form.repos) {
                    if (res.results[repo.id]) repo.commit_sha = res.results[repo.id];
                }
            }
        } finally {
            this.state.freezingAll = false;
        }
    }

    async unfreezeAllRepos() {
        const ids = this.state.form.repos
            .filter(r => r.id && r.commit_sha)
            .map(r => r.id);
        if (ids.length) {
            await rpc("/cloud/unfreeze_all_repos", {
                repo_ids: ids, model: "cloud.instance.repo",
            });
        }
        for (const repo of this.state.form.repos) {
            repo.commit_sha = "";
        }
    }

    async _fetchAndMergeRequirements(url, branch) {
        try {
            const data = await rpc("/cloud/get_repo_requirements", { url, branch });
            if (!data.ok || !data.found || !data.content) return;
            const result = mergeRequirements(this.state.pipDeps, data.content, url, branch);
            this.state.pipDeps = result.content;
            if (result.conflicts.length) {
                this.openPipConflictModal(result.conflicts);
            }
        } catch (e) { console.warn("Failed to fetch repo requirements:", e); }
    }

    goBack() {
        this.env.navigate("project_detail", { project_id: this.props.project_id });
    }

    envLabel(env) {
        return { staging: _t("Staging"), production: _t("Production") }[env] || env;
    }

    // ── Code editor helpers ────────────────────────────────────────────────

    getLineNumbers(field) {
        const text = field === 'pipDeps' ? this.state.pipDeps
                   : field === 'aptDeps' ? this.state.aptDeps
                   : (this.state.form[field] || "");
        const lines = Math.max(1, text.split("\n").length);
        return Array.from({ length: lines }, (_, i) => i + 1);
    }

    onCodeScroll(ev) {
        const gutter = ev.target.previousElementSibling;
        if (gutter && gutter.classList.contains("ic-code-gutter")) {
            gutter.scrollTop = ev.target.scrollTop;
        }
    }

    // ── Instance actions ──────────────────────────────────────────────────

    get statusLabel() {
        const inst = this.state.inst;
        if (!inst) return "";
        if (inst.status === "provisioning") return _t("Building...");
        if (!inst.deployed) return _t("Not deployed");
        if (inst.running) return _t("Running");
        if (inst.status === "error") return _t("Error");
        return _t("Stopped");
    }

    get statusClass() {
        const inst = this.state.inst;
        if (!inst) return "";
        if (inst.status === "provisioning") return "st-provisioning";
        if (!inst.deployed) return "st-pending";
        if (inst.running) return "st-running";
        if (inst.status === "error") return "st-error";
        return "st-stopped";
    }

    // ── Unified job launcher ──────────────────────────────────────────

    /**
     * Central method to enqueue a job and immediately refresh the UI.
     *
     * @param {Function} fn  - async function that enqueues the job and
     *                          returns {ok, job_id, blocked?, error?} or
     *                          a plain job_id (from orm.call).
     * @param {Object} [opts]
     * @param {boolean} [opts.openLog=false]      - open /cloud/log/<id> on success
     * @param {boolean} [opts.goToOverview=false] - jump to the Overview tab on success
     * @param {string}  [opts.errorLabel]         - fallback toast message
     * @returns {Object|null} the response data, or null on error
     */
    async _enqueueJob(fn, { openLog = false, goToOverview = false, errorLabel, loadingKey = "actionLoading" } = {}) {
        if (this.state[loadingKey]) return null;
        this.state[loadingKey] = true;
        try {
            const data = await fn();
            // orm.call returns a plain id; rpc endpoints return {ok, job_id}
            const jobId = typeof data === 'number' ? data : data?.job_id;
            if (data?.ok === false || data?.error) {
                this.env.toast?.error(data.error || errorLabel || _t("Action failed"));
                return null;
            }
            if (data?.blocked) {
                this.state.conflictBlocked = {
                    alert_id: data.alert_id,
                    alert_code: data.alert_code || '',
                    message: data.message,
                    conflicts: data.conflicts || [],
                };
            } else {
                if (openLog && jobId) {
                    window.open(`/cloud/log/${jobId}`, "_blank");
                }
                if (goToOverview) {
                    this.setTab('overview');
                }
            }
            // Immediate refresh so the new job shows in Recent Activity
            this._silentRefresh();
            return data;
        } catch (e) {
            this.env.toast?.error(
                e.data?.message || e.message || errorLabel || _t("Action failed"),
            );
            return null;
        } finally {
            this.state[loadingKey] = false;
        }
    }

    async deployInstance() {
        await this._enqueueJob(
            () => rpc("/cloud/deploy_instance", { instance_id: this.props.instance_id }),
            { openLog: true, errorLabel: _t("Deploy failed") },
        );
    }

    async rebuildInstance() {
        await this._enqueueJob(
            () => rpc("/cloud/rebuild_instance", { instance_id: this.props.instance_id }),
            { openLog: true, errorLabel: _t("Rebuild failed") },
        );
    }

    async instanceAction(action) {
        const inst = this.state.inst;
        if (!inst?.host_id) return;
        await this._enqueueJob(
            () => this.orm.call("cloud.job", "enqueue", [inst.host_id, this.props.instance_id, action]),
        );
    }

    viewLogs() {
        window.open(`/cloud/instance/${this.props.instance_id}/logs`, "_blank");
    }

    async exportInstance() {
        const inst = this.state.inst;
        if (!inst?.host_id) return;
        await this._enqueueJob(
            () => this.orm.call("cloud.job", "enqueue", [inst.host_id, this.props.instance_id, "export_instance"]),
        );
    }

    // ── Restore DB ───────────────────────────────────────────────────────

    openRestore() {
        this.state.restoreDialog = {
            inst: this.state.inst,
            mode: "browser",
            file: null,
            uploading: false,
            copied: false,
            error: null,
        };
    }

    closeRestore() { this.state.restoreDialog = null; }

    onRestoreFileChange(ev) {
        const file = ev.target.files[0];
        if (!file) return;
        const d = this.state.restoreDialog;
        d.file = file;
        d.error = null;
        const TWO_GB = 2 * 1024 * 1024 * 1024;
        if (file.size > TWO_GB) {
            d.mode = "rsync";
            d.error = _t("File exceeds 2 GB — switched to SSH / rsync mode.");
        }
    }

    async copyRsyncCmd() {
        const d = this.state.restoreDialog;
        await navigator.clipboard.writeText(this.rsyncCommand());
        d.copied = true;
        setTimeout(() => { if (d) d.copied = false; }, 2000);
    }

    rsyncCommand() {
        const inst = this.state.inst;
        const user = inst.host_user || "ubuntu";
        const ip = inst.host_ip || inst.host;
        const port = inst.host_port || 22;
        const dest = `/tmp/incubacloud-restore-${this.props.instance_id}.zip`;
        const portFlag = port !== 22 ? ` -e "ssh -p ${port}"` : "";
        return `rsync -P --inplace${portFlag} <local_backup.zip> ${user}@${ip}:${dest}`;
    }

    async doRestore() {
        const d = this.state.restoreDialog;
        if (!d || d.uploading) return;
        d.error = null;
        d.uploading = true;
        try {
            if (d.mode === "browser") {
                if (!d.file) { d.error = _t("Please select a backup file first."); d.uploading = false; return; }
                const fd = new FormData();
                fd.append("backup_file", d.file);
                fd.append("csrf_token", odoo.csrf_token);
                const resp = await fetch(`/cloud/instance/${this.props.instance_id}/restore`, { method: "POST", body: fd });
                if (resp.status === 413) {
                    throw new Error(_t("File too large for browser upload. Please use the SSH / rsync method instead."));
                }
                if (!resp.ok) {
                    let msg = `Server error (${resp.status})`;
                    try { const r = await resp.json(); if (r.error) msg = r.error; } catch {}
                    throw new Error(msg);
                }
                // A 200 with a non-JSON body (typical signal of a
                // session timeout that returned the login page) would
                // otherwise blow up inside resp.json() with a parser
                // error that surfaced as a confusing toast. Detect it
                // here and prompt the user to log in again.
                const ctype = (resp.headers.get('Content-Type') || '').toLowerCase();
                if (!ctype.includes('application/json')) {
                    throw new Error(_t("Session expired. Please reload and try again."));
                }
                const result = await resp.json();
                if (result.error) throw new Error(result.error);
                if (result.job_id) window.open(`/cloud/log/${result.job_id}`, "_blank");
            } else {
                const remote = `/tmp/incubacloud-restore-${this.props.instance_id}.zip`;
                const jobId = await this.orm.call("cloud.instance", "restore_db", [[this.props.instance_id], { mode: "rsync", remote_path: remote }]);
                if (jobId) window.open(`/cloud/log/${jobId}`, "_blank");
            }
            this.state.restoreDialog = null;
        } catch (e) {
            d.error = (e.data?.message || e.message) || _t("Operation failed. Please try again.");
        } finally {
            if (d) d.uploading = false;
        }
    }

    // ── Connect as user ──────────────────────────────────────────────────

    async openConnectModal() {
        this.state.connectDialog = { inst: this.state.inst, loading: true, users: [], error: null, connecting: false };
        try {
            const result = await rpc("/cloud/get_instance_users", { instance_id: this.props.instance_id });
            if (!result.ok) {
                this.state.connectDialog.error = result.error || _t("Failed to load users.");
            } else {
                this.state.connectDialog.users = result.users;
            }
        } catch (e) {
            this.state.connectDialog.error = _t("Connection error.");
        } finally {
            this.state.connectDialog.loading = false;
        }
    }

    closeConnectModal() { this.state.connectDialog = null; }

    async connectAs(userId) {
        const d = this.state.connectDialog;
        if (!d || d.connecting) return;
        d.connecting = true;
        d.error = null;
        try {
            const userEntry = (d.users || []).find(u => u.id === userId);
            const userName = userEntry ? userEntry.name : String(userId);
            const result = await rpc("/cloud/prepare_instance_connect", {
                instance_id: this.props.instance_id,
                user_id: userId,
                user_name: userName,
            });
            if (!result.ok) { d.error = result.error || _t("Failed to create session."); d.connecting = false; return; }
            window.open(result.url, "_blank");
            this.state.connectDialog = null;
        } catch (e) {
            d.error = _t("Connection error.");
            d.connecting = false;
        }
    }

    // ── Shell terminal ───────────────────────────────────────────────────

    openShellDialog() {
        const inst = this.state.inst;
        const raw = inst.compose_services || 'odoo,db';
        const services = Array.isArray(raw) ? raw : raw.split(',').map(s => s.trim()).filter(Boolean);
        if (services.length === 1) {
            this._openShellFor(services[0]);
        } else {
            this.state.shellDialog = { inst, services, opening: false, error: null };
        }
    }

    closeShellDialog() { this.state.shellDialog = null; }

    async openShell(service) {
        const d = this.state.shellDialog;
        if (!d || d.opening) return;
        await this._openShellFor(service);
    }

    async _openShellFor(service) {
        if (this.state.shellDialog) this.state.shellDialog.opening = true;
        try {
            const result = await rpc('/cloud/terminal/open', { instance_id: this.props.instance_id, service });
            if (result.ok) {
                window.open(`/cloud/terminal/${result.session_id}`, '_blank');
                this.state.shellDialog = null;
            } else {
                if (this.state.shellDialog) {
                    this.state.shellDialog.error = result.error || _t('Failed to open terminal.');
                    this.state.shellDialog.opening = false;
                }
            }
        } catch (e) {
            if (this.state.shellDialog) {
                this.state.shellDialog.error = _t('Connection error.');
                this.state.shellDialog.opening = false;
            }
        }
    }

    // ── Sync with project ─────────────────────────────────────────────────

    async openSyncModal() {
        this.state.syncModal = { diff: null, actions: {}, loading: true, applying: false, error: null };
        try {
            const result = await rpc("/cloud/compare_sync", { instance_id: this.props.instance_id });
            if (!result.ok) {
                this.state.syncModal.error = result.error;
                this.state.syncModal.loading = false;
                return;
            }
            this.state.syncModal.diff = result.diff;
            this.state.syncModal.actions = this._buildSyncActions(result.diff);
        } catch (e) {
            this.state.syncModal.error = _t("Failed to compare with project.");
        }
        this.state.syncModal.loading = false;
    }

    closeSyncModal() { this.state.syncModal = null; }

    _buildSyncActions(diff) {
        const actions = { pip: {}, apt: {}, repos: {} };
        // Pre-build action maps: each item gets a default action
        for (const dep of ['pip', 'apt']) {
            actions[dep] = {
                push: {},   // {line: true/false} — push to project
                pull: {},   // {line: true/false} — pull to instance
                conflicts: {},  // {name: 'instance'|'project'|null}
            };
            for (const line of (diff[dep]?.only_instance || [])) {
                actions[dep].push[line] = false;
            }
            for (const line of (diff[dep]?.only_project || [])) {
                actions[dep].pull[line] = false;
            }
            for (const c of (diff[dep]?.conflicts || [])) {
                actions[dep].conflicts[c.name] = null;
            }
        }
        actions.repos = {
            push: {},
            pull: {},
            conflicts: {},
        };
        for (const r of (diff.repos?.only_instance || [])) {
            actions.repos.push[r.url] = false;
        }
        for (const r of (diff.repos?.only_project || [])) {
            actions.repos.pull[r.url] = false;
        }
        for (const c of (diff.repos?.conflicts || [])) {
            actions.repos.conflicts[c.url] = null;
        }
        return actions;
    }

    toggleSyncAction(section, direction, key) {
        const a = this.state.syncModal.actions[section][direction];
        a[key] = !a[key];
    }

    setSyncConflict(section, name, winner) {
        this.state.syncModal.actions[section].conflicts[name] = winner;
    }

    get syncHasSelections() {
        const a = this.state.syncModal?.actions;
        if (!a) return false;
        for (const sec of ['pip', 'apt', 'repos']) {
            for (const v of Object.values(a[sec].push || {})) { if (v) return true; }
            for (const v of Object.values(a[sec].pull || {})) { if (v) return true; }
            for (const v of Object.values(a[sec].conflicts || {})) { if (v) return true; }
        }
        return false;
    }

    get syncHasDifferences() {
        const d = this.state.syncModal?.diff;
        if (!d) return false;
        for (const sec of ['pip', 'apt', 'repos']) {
            if (d[sec]?.only_instance?.length) return true;
            if (d[sec]?.only_project?.length) return true;
            if (d[sec]?.conflicts?.length) return true;
        }
        return false;
    }

    async applySyncActions() {
        const modal = this.state.syncModal;
        if (!modal || modal.applying) return;
        modal.applying = true;
        modal.error = null;
        try {
            // Build payload: only include checked items
            const payload = {};
            for (const sec of ['pip', 'apt', 'repos']) {
                const a = modal.actions[sec];
                payload[sec] = {
                    push: Object.entries(a.push).filter(([, v]) => v).map(([k]) => k),
                    pull: Object.entries(a.pull).filter(([, v]) => v).map(([k]) => k),
                    conflicts: Object.fromEntries(
                        Object.entries(a.conflicts).filter(([, v]) => v)
                    ),
                };
            }
            const result = await rpc("/cloud/apply_sync", {
                instance_id: this.props.instance_id,
                actions: payload,
            });
            if (!result.ok) {
                modal.error = result.error || _t("Sync failed.");
                modal.applying = false;
                return;
            }
            this.state.syncModal = null;
            // Reload instance data to reflect changes
            await this.load();
        } catch (e) {
            modal.error = (e.data?.message || e.message) || _t("Sync failed.");
            modal.applying = false;
        }
    }

    // ── Backups ──────────────────────────────────────────────────────────

    get canShowBackups() {
        const inst = this.state.inst;
        return !!(inst && inst.deployed);
    }

    get isProductionBackup() {
        const inst = this.state.inst;
        return !!(inst && inst.environment === 'production' && inst.effective_backup_backend_id);
    }

    formatBackupDate(isoStr) {
        if (!isoStr) return "—";
        const d = parseUTC(isoStr);
        if (!d || isNaN(d)) return isoStr;
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    formatSize(bytes) {
        if (!bytes) return "—";
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        let size = bytes;
        while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
        return `${size.toFixed(i ? 1 : 0)} ${units[i]}`;
    }

    async _loadBackups() {
        /**Load persisted backup records (instant, no SSH job).*/
        this.state.backupsLoading = true;
        this.state.backupsError = null;
        try {
            const res = await rpc("/cloud/list_backups", { instance_id: this.props.instance_id });
            if (!res.ok) {
                this.state.backupsError = res.error;
                return;
            }
            this.state.backupsData = res.result;
        } catch (e) {
            this.state.backupsError = (e.data?.message || e.message) || _t("Failed to load backups.");
        } finally {
            this.state.backupsLoading = false;
        }
    }

    async refreshBackups() {
        this.state.backupsError = null;
        const data = await this._enqueueJob(
            () => rpc("/cloud/list_backups", {
                instance_id: this.props.instance_id,
                refresh: true,
            }),
            {
                loadingKey: "backupsLoading",
                goToOverview: true,
                errorLabel: _t("Failed to refresh backups"),
            },
        );
        if (data) {
            this.state.backupsJobId = data.job_id;
            this._pollBackupResult();
        }
    }

    async _pollBackupResult() {
        const jobId = this.state.backupsJobId;
        if (!jobId) return;
        const poll = async () => {
            // Component may have unmounted while awaiting the previous
            // RPC. Double-checking inside the tick complements the
            // clearTimeout inside useSafePoll: the clear kills the
            // queued callback, this kills any RPC already in flight.
            if (!this._safePoll.alive) return;
            try {
                const res = await rpc("/cloud/get_backup_result", { job_id: jobId });
                if (!this._safePoll.alive) return;
                if (!res.ok) {
                    this.state.backupsError = res.error || _t("Backup scan failed.");
                    this.state.backupsLoading = false;
                    return;
                }
                if (res.state === 'done') {
                    this.state.backupsData = res.result;
                    this.state.backupsLoading = false;
                    this.state.actionLoading = false;
                    return;
                }
                this._safePoll.schedule(poll, 2000);
            } catch (e) {
                if (!this._safePoll.alive) return;
                this.state.backupsError = _t("Lost connection while scanning.");
                this.state.backupsLoading = false;
                this.state.actionLoading = false;
            }
        };
        this._safePoll.schedule(poll, 1500);
    }

    async createBackup() {
        const msg = _t("Create a backup? This may take several minutes.");
        const ok = await this._confirm({
            title: _t("Create Backup"),
            message: msg,
            confirmLabel: _t("Create Backup"),
        });
        if (!ok) return;
        this.state.backupsError = null;
        const data = await this._enqueueJob(
            () => rpc("/cloud/create_backup", { instance_id: this.props.instance_id }),
            {
                loadingKey: "backupsLoading",
                goToOverview: true,
                errorLabel: _t("Failed to create backup"),
            },
        );
        if (data) {
            this.state.backupsJobId = data.job_id;
            this._pollBackupResult();
        }
    }

    downloadLatestBackup() {
        // Open the download modal immediately with a 'latest' marker. The
        // selected job (download_backup / download_backup_neutralized)
        // accepts time='latest' and resolves the most recent backup
        // server-side, so the user does not wait for a list_backups scan
        // before picking dump type and filestore.
        this.openDownloadModal('latest', null);
    }

    // Download modal: Type of dump (Neutralized/Exact) × Filestore (without/with).
    // Defaults mirror odoo.sh: Neutralized + Without filestore.
    openDownloadModal(backupTime, attachmentId = null) {
        this.state.downloadModal = {
            time: backupTime,
            attachmentId: attachmentId,
            mode: 'neutral',
            type: 'dump',
            loading: false,
        };
    }

    closeDownloadModal() { this.state.downloadModal = null; }

    get isLiveNeutralDump() {
        const d = this.state.downloadModal;
        if (!d) return false;
        const isProd = this.state.inst?.environment === 'production';
        return d.mode === 'neutral' && !isProd;
    }

    get isLiveExactDump() {
        // Non-prod + Exact + no pre-built attachment → backend takes a
        // live dump on demand via click-odoo-backupdb (no historical
        // snapshot store exists on staging/dev).  Used to swap the
        // "Latest available backup" copy for something accurate.
        const d = this.state.downloadModal;
        if (!d) return false;
        const isProd = this.state.inst?.environment === 'production';
        return d.mode === 'exact' && !isProd && !d.attachmentId;
    }

    async doDownloadBackup() {
        const d = this.state.downloadModal;
        if (!d || d.loading) return;

        // Exact + attachment shortcut: pre-built non-prod ZIPs are served
        // directly without going through a job. The attachment already
        // contains dump + filestore; the Filestore radio is ignored in
        // this case.
        if (d.mode === 'exact' && d.attachmentId) {
            window.location.href = `/web/content/${d.attachmentId}?download=true`;
            this.state.downloadModal = null;
            return;
        }

        d.loading = true;
        try {
            let res;
            if (d.mode === 'neutral') {
                // Non-prod has no historical DB snapshots on the host;
                // always dump the live DB before neutralizing.
                const isProd = this.state.inst?.environment === 'production';
                res = await rpc("/cloud/download_backup_neutralized", {
                    instance_id: this.props.instance_id,
                    time: isProd ? d.time : 'live',
                    with_filestore: d.type === 'all',
                });
            } else {
                res = await rpc("/cloud/download_backup", {
                    instance_id: this.props.instance_id,
                    time: d.time,
                    download_type: d.type,
                });
            }
            if (!res.ok) {
                this.state.backupsError = res.error;
                d.loading = false;
                return;
            }
            this.state.downloadModal = null;
            // The download is an enqueued job whose final ZIP lands as
            // an ir.attachment on cloud.job — surface it on Overview so
            // the user sees the job and grabs the file from there.
            this._silentRefresh();
            this.setTab('overview');
        } catch (e) {
            this.state.backupsError = (e.data?.message || e.message) || _t("Download failed.");
            d.loading = false;
        }
    }

    // Restore confirmation for production
    openRestoreModal(backupTime) {
        this.state.restoreBackupModal = {
            time: backupTime,
            confirmName: '',
            loading: false,
        };
    }

    closeRestoreBackupModal() { this.state.restoreBackupModal = null; }

    get restoreNameMatches() {
        const m = this.state.restoreBackupModal;
        return m && m.confirmName.trim() === (this.state.inst?.name || '');
    }

    async doRestoreBackup() {
        const m = this.state.restoreBackupModal;
        if (!m || m.loading || !this.restoreNameMatches) return;
        m.loading = true;
        const time = m.time;
        const data = await this._enqueueJob(
            () => rpc("/cloud/restore_backup", { instance_id: this.props.instance_id, time }),
            {
                openLog: true,
                goToOverview: true,
                errorLabel: _t("Restore failed"),
            },
        );
        if (data) {
            this.state.restoreBackupModal = null;
        } else {
            m.loading = false;
        }
    }

    // ── Confirm + Delete ─────────────────────────────────────────────────

    _confirm({ title, message, confirmLabel = _t("Confirm"), isDanger = false }) {
        return new Promise((resolve) => {
            this.state.confirmDialog = {
                title, message, confirmLabel, isDanger,
                onConfirm: () => { this.state.confirmDialog = null; resolve(true); },
                onCancel: () => { this.state.confirmDialog = null; resolve(false); },
            };
        });
    }

    async deleteInstance() {
        const inst = this.state.inst;
        const confirmed = await this._confirm(inst.deployed ? {
            title: _t("Archive Instance"),
            message: _t("Remove \"%s\" from the host and archive it? Containers will be stopped and the directory deleted.").replace('%s', inst.name),
            confirmLabel: _t("Archive"),
            isDanger: true,
        } : {
            title: _t("Delete Instance"),
            message: _t("Permanently delete \"%s\"? It has never been deployed so no SSH cleanup is needed.").replace('%s', inst.name),
            confirmLabel: _t("Delete"),
            isDanger: true,
        });
        if (!confirmed) return;
        if (inst.deployed) {
            const data = await this._enqueueJob(
                () => this.orm.call("cloud.job", "enqueue", [
                    inst.host_id, this.props.instance_id, "delete_instance",
                ]),
            );
            if (data) this._waitForDeleteJob(data);
        } else {
            try {
                await rpc("/cloud/delete_instance", { instance_id: this.props.instance_id });
                this._navigateAfterDelete();
            } catch (e) {
                this.env.toast?.error(e.data?.message || e.message || _t("Delete failed"));
            }
        }
    }

    _waitForDeleteJob(jobId) {
        const poll = async () => {
            if (!this._safePoll.alive) return;
            try {
                const [job] = await this.orm.call(
                    "cloud.job", "load_jobs", [jobId],
                );
                if (!this._safePoll.alive) return;
                if (job && ['done', 'failed'].includes(job.state)) {
                    this._navigateAfterDelete();
                    return;
                }
                this._safePoll.schedule(poll, 3000);
            } catch (_) {
                this._safePoll.schedule(poll, 3000);
            }
        };
        this._safePoll.schedule(poll, 2000);
    }

    async _loadAuditLog() {
        this.state.auditLogLoading = true;
        const f = this.state.auditFilter;
        try {
            const entries = await rpc("/cloud/get_audit_log", {
                instance_id: this.props.instance_id,
                q: f.q || null,
                action_filter: f.action || null,
                date_from: f.date_from || null,
                date_to: f.date_to || null,
            });
            this.state.auditLog = entries || [];
            // Build action list from first unfiltered load
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

    async _navigateAfterDelete() {
        // Reload so the deleted row is gone from the store before the
        // destination route reads it. The store's bus subscription
        // would catch it eventually, but the user clicks "delete" and
        // navigates immediately — we can't race the debouncer.
        await this.env.projectStore?.load(this.props.project_id);
        const next = this.env.projectStore?.getNextInstance(this.props.instance_id);
        if (next) {
            this.env.navigate("instance_detail", {
                project_id: this.props.project_id,
                instance_id: next.id,
            });
        } else {
            this.env.navigate("project_detail", { project_id: this.props.project_id });
        }
    }
}
