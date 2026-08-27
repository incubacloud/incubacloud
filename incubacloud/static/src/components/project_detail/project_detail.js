import { Component, useState, onWillStart, onMounted, onWillUnmount, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { TagSelector } from "../tag_selector/tag_selector";
import { RepoEditor } from "../repo_editor/repo_editor";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { IcModal } from "../ic_modal/ic_modal";
import { RlSelect } from "../rl_select/rl_select";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";
import { required } from "../../utils/validators";
import { fetchOdooVersions } from "../../utils/odoo_versions";
import { confirmVia } from "../../utils/use_confirm";
import { parseUTC } from "../../utils/dates";
import {
    copyStateBadgeClass,
    copyStateLabel,
    formatBytes,
    isRevivable,
} from "../../utils/archive_copy";

let _nextKey = 1;
const newRepoKey = () => `new_${_nextKey++}`;

// ── Pip requirements helpers ─────────────────────────────────────────────────

function _parseReqLine(line) {
    const stripped = line.split('#')[0].trim();
    if (!stripped) return null;
    const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
    if (!m) return null;
    const name = m[1].toLowerCase();
    // Include environment marker in key so conditional specs don't conflict:
    //   pymssql<=2.5 ; python_version <= '3.10'   ← different entries
    //   pymssql<=2.2.8 ; python_version > '3.10'
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
            // Function form to avoid ``$`` substitution patterns —
            // see instance_detail.js for the same pattern.
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

export class ProjectDetail extends Component {
    static props = {
        project_id: { type: Number, optional: true },
        embedded:   { type: Boolean, optional: true },
    };
    static template = "incubacloud.ProjectDetail";
    static components = { TagSelector, RepoEditor, IcConfirmDialog, IcModal, RlSelect };

    get isCreate() { return !this.props.project_id; }
    get odooVersions() { return this.state.odooVersions; }

    /**
     * Backup backend options for the RlSelect, derived from loaded backends.
     * @returns {{value: number, label: string}[]} option list (numeric ids preserved)
     */
    get backupBackendOptions() {
        return this.state.backupBackends.map((bb) => ({ value: bb.id, label: bb.name }));
    }
    get showOnboarding() {
        if (this.env.currentRoute?.route === "project_settings") return false;
        return this.props.embedded && !this.isCreate
            && this.state.project && this.state.project.instance_count === 0;
    }

    get hasUnsavedChanges() {
        return this._savedForm && JSON.stringify(this.state.form) !== this._savedForm;
    }

    get isValid() {
        // The submit button uses ``t-att-disabled="!isValid || ..."`` —
        // we need a getter that re-evaluates on every render WITHOUT
        // touching the validator's reactive state (otherwise opening
        // a fresh form would immediately paint every required field
        // red before the user has typed anything).
        return this.validator.isValid(this.state.form);
    }

    setup() {
        this.env = useEnv();
        this.state = useState({
            tab: "general",
            odooVersions: [],
            loading: true,
            saving: false,
            error: null,
            project: null,
            form: { name: "", description: "" },
            selectedTags: [],
            allTags: [],
            backupBackends: [],
            repos: [],
            pipDeps: "",
            aptDeps: "",
            pipConflictModal: null,  // { conflicts, choices }
            members: [],
            memberSearch: '',
            memberResults: [],
            confirmDialog: null,
            repoSearch: '',
            // Initial language picker
            langSearch: "",
            langOpen: false,
            langs: [],
            langsLoading: false,
            // Archived instances: loaded with the project so the tab can
            // hide itself when there are none, which is the normal case.
            archived: [],
            archivedError: null,
            // { instance, hostId, hosts, loading, submitting } while the
            // revive dialog is open, null otherwise.
            reviveModal: null,
            // { instance, typed, submitting } while the delete dialog is
            // open. Typed confirmation, same as deleting a live one.
            deleteArchivedModal: null,
        });

        this._savedForm = null;

        this.validator = useFormValidation(() => ({
            name: [required(_t("Project name is required"))],
            odoo_version: [required(_t("Odoo version is required"))],
        }));

        onWillStart(() => this.loadProject());
        useNavGuard(
            () => this.hasUnsavedChanges,
            (opts) => this._confirm(opts),
        );
    }

    async loadProject() {
        this.state.loading = true;
        this.state.odooVersions = await fetchOdooVersions();
        try {
            if (this.isCreate) {
                const [tagsRes, backends] = await Promise.all([
                    rpc("/cloud/get_tags", {}),
                    rpc("/cloud/get_backup_backends", {}),
                ]);
                this.state.allTags = tagsRes.tags || [];
                this.state.backupBackends = backends?.items || backends || [];
                const defs = tagsRes.project_defaults || {};
                this.state.pipDeps = defs.pip_dependencies || "";
                this.state.form = {
                    name: "",
                    description: "",
                    project_author: "IncubaCloud",
                    project_license: "BSL-1.0",
                    odoo_version: defs.odoo_version || this.state.odooVersions.at(-1),
                    odoo_initial_lang: null,
                    backup_backend_id: null,
                };
                this._savedForm = JSON.stringify(this.state.form);
            } else {
                const [p, backends, archived] = await Promise.all([
                    rpc("/cloud/get_project", { project_id: this.props.project_id }),
                    rpc("/cloud/get_backup_backends", {}),
                    // Rides along with the project load rather than
                    // firing when the tab is clicked: the tab only
                    // exists if the answer is non-empty, so the list has
                    // to be known before the tab bar renders.
                    this._fetchArchived(),
                ]);
                this.state.project = p;
                this.env.setProjectName?.(p.name || "");
                this.state.backupBackends = backends?.items || backends || [];
                this.state.form = {
                    name: p.name || "",
                    description: p.description || "",
                    project_author: p.project_author || "IncubaCloud",
                    project_license: p.project_license || "BSL-1.0",
                    odoo_version: p.odoo_version || this.state.odooVersions.at(-1),
                    odoo_initial_lang: p.odoo_initial_lang || null,
                    backup_backend_id: p.backup_backend_id || null,
                };
                this.state.selectedTags = [...(p.tags || [])];
                this.state.allTags = [...(p.all_tags || [])];
                this.state.repos = p.repos.map(r => ({
                    _key: `srv_${r.id}`,
                    id: r.id,
                    url: r.url,
                    branch: r.branch,
                    addons: r.addons || '',
                    excludes: r.excludes || '',
                    commit_sha: r.commit_sha || '',
                }));
                this.state.pipDeps = p.pip_dependencies || "";
                this.state.aptDeps = p.apt_dependencies || "";
                this.state.members = p.member_ids || [];
                this.state.archived = archived;
                this._savedForm = JSON.stringify(this.state.form);
            }
        } catch (e) {
            this.state.error = this.isCreate ? _t("Failed to load form data.") : _t("Failed to load project.");
        } finally {
            this.state.loading = false;
        }
    }

    setTab(tab) { this.state.tab = tab; }

    onInput(field, ev) {
        this.state.form[field] = ev.target.value;

    }

    get filteredRepos() {
        const q = (this.state.repoSearch || '').toLowerCase();
        if (!q) return this.state.repos;
        return this.state.repos.filter(r =>
            (r.url || '').toLowerCase().includes(q)
        );
    }

    onBackendChange(ev) {
        const val = ev.target.value;
        this.state.form.backup_backend_id = val ? parseInt(val, 10) : null;

    }

    // ── Initial language picker ───────────────────────────────────────────────

    async _ensureLangs() {
        if (this.state.langs.length) return;
        this.state.langsLoading = true;
        try {
            const langs = await rpc("/cloud/get_langs");
            this.state.langs = langs;
            if (this.state.form.odoo_initial_lang) {
                const found = langs.find(l => l.id === this.state.form.odoo_initial_lang);
                this.state.langSearch = found ? `${found.name} (${found.code})` : "";
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

    onDepsInput(ev) {
        this.state.pipDeps = ev.target.value;

    }

    onAptInput(ev) {
        this.state.aptDeps = ev.target.value;

    }

    // ── Tag management ───────────────────────────────────────────────────────

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
        const tag = await rpc("/cloud/create_tag", { name });
        if (!this.state.allTags.find(t => t.id === tag.id)) {
            this.state.allTags.push(tag);
        }
        return tag;
    }

    // ── Repo list management ─────────────────────────────────────────────────

    addRepo() {
        this.state.repos.unshift({ _key: newRepoKey(), id: null, url: "", branch: "main", addons: "", excludes: "", commit_sha: "" });

    }

    removeRepo(key) {
        const idx = this.state.repos.findIndex(r => r._key === key);
        if (idx !== -1) this.state.repos.splice(idx, 1);

    }

    onRepoChange(key, field, value) {
        const repo = this.state.repos.find(r => r._key === key);
        if (repo) repo[field] = value;

        if (field === 'branch' && value && repo && repo.url) {
            this._fetchAndMergeRequirements(repo.url, value);
        }
    }

    async freezeAllRepos() {
        const ids = this.state.repos
            .filter(r => r.id && r.url && r.branch && !r.commit_sha)
            .map(r => r.id);
        if (!ids.length) return;
        this.state.freezingAll = true;
        try {
            const res = await rpc("/cloud/freeze_all_repos", {
                repo_ids: ids, model: "cloud.project.repo",
            });
            if (res.ok) {
                for (const repo of this.state.repos) {
                    if (res.results[repo.id]) repo.commit_sha = res.results[repo.id];
                }
            }
        } finally {
            this.state.freezingAll = false;
        }
    }

    async unfreezeAllRepos() {
        const ids = this.state.repos
            .filter(r => r.id && r.commit_sha)
            .map(r => r.id);
        if (ids.length) {
            await rpc("/cloud/unfreeze_all_repos", {
                repo_ids: ids, model: "cloud.project.repo",
            });
        }
        for (const repo of this.state.repos) {
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

    // ── Pip conflict modal ───────────────────────────────────────────────────

    openPipConflictModal(conflicts) {
        const choices = {};
        for (const c of conflicts) {
            choices[c.name] = c.existing;
        }
        this.state.pipConflictModal = { conflicts, choices };
    }

    closePipConflictModal() {
        this.state.pipConflictModal = null;
    }

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

    // ── Save ─────────────────────────────────────────────────────────────────

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
                backup_backend_id: this.state.form.backup_backend_id || null,
                tag_ids: this.state.selectedTags.map(t => t.id),
                repos: this.state.repos.map(r => ({
                    url: r.url,
                    branch: r.branch || "main",
                    addons: r.addons || "",
                    excludes: r.excludes || "",
                    commit_sha: r.commit_sha || "",
                })),
                pip_dependencies: this.state.pipDeps,
                apt_dependencies: this.state.aptDeps,
                member_ids: this.state.members.map(m => m.id),
            };
            if (this.isCreate) {
                const result = await rpc("/cloud/create_project", { vals });
                // Clear the dirty baseline before navigating so the
                // nav guard doesn't fire on this programmatic
                // transition (matches the pattern HostDetail.save()
                // already uses for the new-host create path).
                this._savedForm = JSON.stringify(this.state.form);
                this.env.navigate("project_detail", { project_id: result.id });
            } else {
                await rpc("/cloud/save_project", {
                    project_id: this.props.project_id,
                    vals,
                });
                this.state.project.name = this.state.form.name;
                this.env.toast?.success(_t("Changes saved"));
                this._savedForm = JSON.stringify(this.state.form);
                this.env.refreshAlertCount();
            }
        } catch (e) {
            this.env.toast?.error(this.isCreate ? _t("Failed to create project.") : _t("Failed to save project."));
        } finally {
            this.state.saving = false;
        }
    }

    goBack() { this.env.navigate("projects"); }

    // ── Members ──────────────────────────────────────────────────────────────

    searchMembers(query) {
        this.state.memberSearch = query;
        if (!query || query.length < 2) {
            this.state.memberResults = [];
            return;
        }
        clearTimeout(this._memberSearchTimeout);
        this._memberSearchTimeout = setTimeout(async () => {
            const results = await rpc('/cloud/get_users', { search: query });
            const currentIds = new Set(this.state.members.map(m => m.id));
            this.state.memberResults = results.filter(u => !currentIds.has(u.id));
        }, 300);
    }

    addMember(user) {
        if (!this.state.members.some(m => m.id === user.id)) {
            this.state.members.push({ id: user.id, name: user.name });
        }
        this.state.memberSearch = '';
        this.state.memberResults = [];

    }

    removeMember(userId) {
        const member = this.state.members.find(m => m.id === userId);
        this.state.members = this.state.members.filter(m => m.id !== userId);

        if (member) {
        }
    }

    // ── Archived instances ────────────────────────────────────────────────────

    /**
     * Fetch this project's archived instances.
     *
     * Never rejects. It runs inside the ``Promise.all`` that loads the
     * project, and an archived-instance listing failing is not a reason
     * for the project page to show "Failed to load project" — the tab
     * simply does not appear, and the error is kept for the banner.
     *
     * @returns {Promise<object[]>} rows, or an empty list on failure.
     */
    async _fetchArchived() {
        if (this.isCreate || !this.props.project_id) return [];
        try {
            const res = await rpc("/cloud/get_archived_instances", {
                project_id: this.props.project_id,
            });
            if (!res?.ok) {
                this.state.archivedError = res?.error || _t("Failed to load archived instances.");
                return [];
            }
            this.state.archivedError = null;
            return res.instances || [];
        } catch {
            this.state.archivedError = _t("Failed to load archived instances.");
            return [];
        }
    }

    /** Re-read the archived list, e.g. after a revive was launched. */
    async refreshArchived() {
        this.state.archived = await this._fetchArchived();
    }

    /** Human-readable size of an archived chain. */
    archivedSize(inst) { return formatBytes(inst.copy_bytes); }

    /** Label for the copy-state badge of one archived row. */
    archivedCopyLabel(inst) { return copyStateLabel(inst.copy_state); }

    /** ``rl-sb`` modifier for the copy-state badge of one archived row. */
    archivedCopyClass(inst) { return copyStateBadgeClass(inst.copy_state); }

    /** Whether the Revive button should be offered for one archived row. */
    canRevive(inst) { return isRevivable(inst); }

    /**
     * "checked 3h ago", or an explicit "never checked".
     *
     * The age is part of the claim, not decoration: a badge saying
     * "Present" is only worth as much as the timestamp behind it, and a
     * copy nothing has verified yet must not read as verified.
     */
    archivedCheckedAgo(inst) {
        if (!inst.copy_checked_at) return _t("never checked");
        const secs = Math.floor(
            (Date.now() - parseUTC(inst.copy_checked_at).getTime()) / 1000
        );
        if (secs < 60) return _t("checked just now");
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${_t("checked")} ${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${_t("checked")} ${hrs}h ago`;
        return `${_t("checked")} ${Math.floor(hrs / 24)}d ago`;
    }

    /** Short local date for the archived-on column. */
    archivedOn(inst) {
        const d = parseUTC(inst.archived_on);
        if (!d) return "—";
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }

    /**
     * Open the revive dialog for one archived instance.
     *
     * The host list is fetched here and not on project load: reviving is
     * rare, and every project view would otherwise pay for an RPC almost
     * nobody uses.
     */
    async openRevive(inst) {
        this.state.reviveModal = {
            instance: inst,
            hostId: null,
            hosts: [],
            loading: true,
            submitting: false,
        };
        try {
            const res = await rpc("/cloud/get_hosts", {});
            // Same filter the create-instance form applies: reviving
            // deploys a real instance, so a host that cannot receive one
            // must not be offered here either.
            const hosts = (res?.hosts || []).filter(
                h => h.status === "compatible" && h.traefik_deployed
            );
            if (!this.state.reviveModal) return;   // closed while loading
            this.state.reviveModal.hosts = hosts;
            // Pre-select the host it was archived from when it is still
            // around, so the common case is one click.
            const previous = hosts.find(h => h.name === inst.host_name);
            this.state.reviveModal.hostId = previous ? previous.id : null;
        } catch {
            this.env.toast?.error(_t("Failed to load hosts."));
        } finally {
            if (this.state.reviveModal) this.state.reviveModal.loading = false;
        }
    }

    /** Close the revive dialog without doing anything. */
    closeRevive() { this.state.reviveModal = null; }

    /** Options for the host picker inside the revive dialog. */
    get reviveHostOptions() {
        const hosts = this.state.reviveModal?.hosts || [];
        return hosts.map(h => ({
            value: h.id,
            label: `${h.name} (${h.available_cpus} CPU / ${h.available_ram_gb} GB free)`,
        }));
    }

    /** Record the host chosen in the revive dialog. */
    setReviveHost(value) {
        if (this.state.reviveModal) this.state.reviveModal.hostId = value;
    }

    /**
     * Launch the revive: redeploy on the chosen host, then restore.
     *
     * The server re-probes the copy before enqueuing anything, so a
     * refusal here is a real answer about the storage and is surfaced
     * verbatim rather than flattened into a generic failure.
     */
    async confirmRevive() {
        const modal = this.state.reviveModal;
        if (!modal || modal.submitting) return;
        if (!modal.hostId) {
            this.env.toast?.error(_t("Pick a host to revive this instance on."));
            return;
        }
        modal.submitting = true;
        try {
            const res = await rpc("/cloud/revive_instance", {
                instance_id: modal.instance.id,
                host_id: modal.hostId,
            });
            if (!res?.ok) {
                this.env.toast?.error(res?.error || _t("Failed to revive instance."));
                modal.submitting = false;
                return;
            }
            this.state.reviveModal = null;
            this.env.toast?.success(
                _t("Reviving %s: deploying, then restoring its copy.")
                    .replace("%s", modal.instance.name)
            );
            await this.refreshArchived();
            this.env.navigate("instance_detail", {
                project_id: this.props.project_id,
                instance_id: modal.instance.id,
            });
        } catch {
            this.env.toast?.error(_t("Failed to revive instance."));
            if (this.state.reviveModal) this.state.reviveModal.submitting = false;
        }
    }

    /** Open the delete dialog for one archived instance. */
    openDeleteArchived(inst) {
        this.state.deleteArchivedModal = {
            instance: inst,
            typed: "",
            submitting: false,
        };
    }

    /** Close the delete dialog without deleting anything. */
    closeDeleteArchived() { this.state.deleteArchivedModal = null; }

    /** Record what the operator typed into the delete dialog. */
    onDeleteArchivedInput(ev) {
        if (this.state.deleteArchivedModal) {
            this.state.deleteArchivedModal.typed = ev.target.value;
        }
    }

    /**
     * Whether what was typed unlocks the delete button.
     *
     * The name, and exactly the name — no trimming, no case folding.
     * Same reasoning as deleting a live instance: the expensive mistake
     * is not "I did not mean to delete", it is "I deleted the wrong
     * one", and only typing the name catches that. A barrier that
     * forgives is a barrier you clear on autopilot.
     */
    get deleteArchivedConfirmed() {
        const modal = this.state.deleteArchivedModal;
        return !!modal && modal.typed === modal.instance.name;
    }

    /**
     * Delete an archived instance and the copy it owns.
     *
     * The server empties the prefix first and unlinks the record only
     * once that succeeded, so a refusal here means the copy is still
     * there — and so is the row.
     */
    async confirmDeleteArchived() {
        const modal = this.state.deleteArchivedModal;
        if (!modal || modal.submitting || !this.deleteArchivedConfirmed) return;
        modal.submitting = true;
        try {
            const res = await rpc("/cloud/delete_archived_instance", {
                instance_id: modal.instance.id,
                confirm_name: modal.typed,
            });
            if (!res?.ok) {
                this.env.toast?.error(
                    res?.error || _t("Failed to delete archived instance.")
                );
                modal.submitting = false;
                return;
            }
            this.state.deleteArchivedModal = null;
            this.env.toast?.success(
                res.job_ids?.length
                    ? _t("Deleting %s: clearing its copy first.")
                        .replace("%s", modal.instance.name)
                    : _t("%s deleted.").replace("%s", modal.instance.name)
            );
            await this.refreshArchived();
            if (!this.state.archived.length) this.setTab("general");
        } catch {
            this.env.toast?.error(_t("Failed to delete archived instance."));
            if (this.state.deleteArchivedModal) {
                this.state.deleteArchivedModal.submitting = false;
            }
        }
    }

    // ── Confirm dialog ────────────────────────────────────────────────────────

    /** Open the shared confirmation dialog. See utils/use_confirm.js. */
    _confirm(opts) {
        return confirmVia(this.state, opts);
    }

    async deleteProject() {
        const ok = await this._confirm({
            title: _t("Delete project"),
            message: _t("Are you sure you want to delete \"%s\"? This action cannot be undone.").replace('%s', this.state.form.name),
            confirmLabel: _t("Delete"),
            isDanger: true,
        });
        if (!ok) return;
        try {
            const res = await rpc("/cloud/delete_project", { project_id: this.props.project_id });
            if (res.error) {
                this.env.toast?.error(res.error);
                return;
            }
            this.env.navigate("projects");
        } catch {
            this.env.toast?.error(_t("Failed to delete project."));
        }
    }

    createInstance(environment) {
        this.env.navigate("create_instance", {
            project_id: this.props.project_id,
            environment,
        });
    }

    statusLabel(status) {
        return { ok: _t("OK"), warning: _t("Warning"), error: _t("Error") }[status] || status;
    }

    /**
     * Map a project status to the matching ``rl-sb`` state-badge CSS class.
     * View-helper only (no state change); used by the header status badge.
     * @param {string} status project status (``ok`` / ``warning`` / ``error``)
     * @returns {string} the ``rl-sb`` modifier class (``ok`` / ``warn`` / ``failed``)
     */
    statusBadgeClass(status) {
        return { ok: "ok", warning: "warn", error: "failed" }[status] || "ok";
    }

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
}
