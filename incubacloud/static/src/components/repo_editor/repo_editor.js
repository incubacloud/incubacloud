import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { ModuleSelector } from "../module_selector/module_selector";
import { SearchSelect } from "../search_select/search_select";

/**
 * RepoEditor — single repo card with GitHub-backed branch and module selectors.
 *
 * Props:
 *   repo      — { id, url, branch, addons?, excludes?, commit_sha? }
 *   hasAddons — bool (true for instance repos, false for project repos)
 *   onChange  — (field, value) => void
 *   onRemove  — () => void
 */
export class RepoEditor extends Component {
    static template = "incubacloud.RepoEditor";
    static components = { SearchSelect, ModuleSelector };
    static props = {
        repo:               { type: Object },
        hasAddons:          { type: Boolean },
        onChange:           { type: Function },
        onRemove:           { type: Function },
        preferredBranch:    { type: String, optional: true },
        onFetchRequirements: { type: Function, optional: true },
        repoModel:          { type: String, optional: true },
        hideFreeze:         { type: Boolean, optional: true },
    };

    setup() {
        this.state = useState({
            branches:        [],
            modules:         [],
            loadingBranches: false,
            loadingModules:  false,
            branchError:     null,
            branchSettingsHint: false,
            moduleError:     null,
            freezing:        false,
        });
    }

    get isFrozen() {
        return !!this.props.repo.commit_sha;
    }

    get shortSha() {
        return (this.props.repo.commit_sha || "").slice(0, 7);
    }

    // ── GitHub fetchers ────────────────────────────────────────────────────

    async _fetchBranches(url) {
        if (!url) return;
        this.state.loadingBranches = true;
        this.state.branchError = null;
        this.state.branchSettingsHint = false;
        this.state.branches = [];
        try {
            const data = await rpc("/cloud/get_repo_branches", { url });
            this.state.branches = data.branches || [];
            if (!data.ok && data.error) {
                this.state.branchError = data.error;
                this.state.branchSettingsHint = !!data.settings_hint;
            }
        } catch {
            this.state.branchError = "Could not reach Odoo server";
        } finally {
            this.state.loadingBranches = false;
        }
    }

    async _fetchModules(url, branch) {
        if (!url || !branch) return;
        this.state.loadingModules = true;
        this.state.moduleError = null;
        this.state.modules = [];
        try {
            const data = await rpc("/cloud/get_repo_modules", { url, branch });
            this.state.modules = data.modules || [];
            if (!data.ok && data.error) {
                this.state.moduleError = data.error;
            }
        } catch {
            this.state.moduleError = "Could not reach Odoo server";
        } finally {
            this.state.loadingModules = false;
        }
    }

    // ── URL ────────────────────────────────────────────────────────────────

    onUrlInput(ev) {
        this.props.onChange("url", ev.target.value);
    }

    async onUrlBlur(ev) {
        const url = (ev.target.value || "").trim();
        if (!url) return;
        // Normalise URL in parent state first so repo.url is correct when
        // onBranchChange fires and the parent calls _fetchAndMergeRequirements.
        this.props.onChange("url", url);
        // Reset branch/addons/excludes and reload branches whenever URL changes
        this.props.onChange("branch", "");
        if (this.props.hasAddons) {
            this.props.onChange("addons", "");
            this.props.onChange("excludes", "");
        }
        this.state.modules = [];
        await this._fetchBranches(url);
        // Auto-select best branch so requirements fetch is always triggered.
        // Priority: preferredBranch (project version) → main → master → first.
        const branches = this.state.branches;
        if (branches.length) {
            const preferred = this.props.preferredBranch;
            const best = (preferred && branches.includes(preferred) ? preferred : null)
                ?? ["main", "master"].find(b => branches.includes(b))
                ?? branches[0];
            await this.onBranchChange(best);
            // Call explicit callback with the resolved url+branch so the parent
            // can fetch requirements without relying on reactive state timing.
            this.props.onFetchRequirements?.(url, best);
        }
    }

    // ── Branch ────────────────────────────────────────────────────────────

    async onBranchOpen() {
        if (!this.state.branches.length && !this.state.loadingBranches && this.props.repo.url) {
            await this._fetchBranches(this.props.repo.url);
        }
    }

    async onBranchChange(branch) {
        this.props.onChange("branch", branch);
        if (this.props.hasAddons) {
            this.props.onChange("addons", "");
            this.props.onChange("excludes", "");
            this.state.modules = [];
            this.state.moduleError = null;
            if (branch) await this._fetchModules(this.props.repo.url, branch);
        }
    }

    // ── Module selectors ───────────────────────────────────────────────────

    /** Set of module names currently in the "exclude" list. */
    get excludedSet() {
        return new Set(
            (this.props.repo.excludes || "")
                .split(",").map(s => s.trim()).filter(Boolean)
        );
    }

    /** Set of module names currently in the "include" list. */
    get includedSet() {
        return new Set(
            (this.props.repo.addons || "")
                .split(",").map(s => s.trim()).filter(Boolean)
        );
    }

    /** Lazy-load modules when a ModuleSelector is first opened. */
    async onModuleOpen() {
        if (
            !this.state.modules.length &&
            !this.state.loadingModules &&
            this.props.repo.url &&
            this.props.repo.branch
        ) {
            await this._fetchModules(this.props.repo.url, this.props.repo.branch);
        }
    }

    // ── Freeze / Unfreeze ───────────────────────────────────────────────

    async toggleFreeze() {
        const { id, url, branch } = this.props.repo;
        const model = this.props.repoModel;
        // Freeze/HEAD failures are not credential issues — clear any stale hint.
        this.state.branchSettingsHint = false;

        if (this.isFrozen) {
            // Unfreeze
            if (id && model) {
                await rpc("/cloud/unfreeze_repo", { repo_id: id, model });
            }
            this.props.onChange("commit_sha", "");
            return;
        }

        if (!url || !branch) return;
        this.state.freezing = true;
        try {
            let sha;
            if (id && model) {
                // Saved repo — freeze directly in DB
                const res = await rpc("/cloud/freeze_repo", { repo_id: id, model });
                if (res.ok) {
                    sha = res.sha;
                } else {
                    this.state.branchError = res.error || "Could not freeze";
                    return;
                }
            } else {
                // Unsaved repo — just fetch SHA locally
                const res = await rpc("/cloud/get_branch_head", { url, branch });
                if (res.ok) {
                    sha = res.sha;
                } else {
                    this.state.branchError = res.error || "Could not fetch HEAD";
                    return;
                }
            }
            this.props.onChange("commit_sha", sha);
        } catch {
            this.state.branchError = "Could not reach server";
        } finally {
            this.state.freezing = false;
        }
    }
}
