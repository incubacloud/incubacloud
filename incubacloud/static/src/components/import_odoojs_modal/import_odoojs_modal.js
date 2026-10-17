import { Component, useState, useRef } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { SearchSelect } from "../search_select/search_select";
import { useDialogA11y } from "../../utils/use_dialog_a11y";

/** Convert git@github.com:owner/repo.git → https://github.com/owner/repo.git */
function toHttpsUrl(url) {
    if (!url) return url;
    url = url.trim();
    const sshMatch = url.match(/^git@github\.com:(.+?)(?:\.git)?$/);
    if (sshMatch) return `https://github.com/${sshMatch[1]}.git`;
    return url;
}

export class ImportProjectModal extends Component {
    static template = "incubacloud.ImportProjectModal";
    static components = { SearchSelect };

    static props = {
        onClose: Function,
        onCreated: Function,
    };

    setup() {
        this.state = useState({
            step: "input",  // input | progress | done | error
            repoUrl: "",
            branches: [],
            // The URL ``branches`` was fetched for.
            branchesFor: null,
            branch: "",
            loadingBranches: false,
            branchError: null,
            error: null,
            // Progress messages
            messages: [],
            // Result
            result: null,
        });
        this.data = useState(this.env.dialogData);
        this.boxRef = useRef("box");
        useDialogA11y(this.boxRef, () => this.close());
    }

    async close() {
        if (this.state.step === "progress") return;
        if (this.data.dismiss) await this.data.dismiss();
        if (this.props.onClose) this.props.onClose();
        return this.data.close({ dismiss: true });
    }

    onOverlayClick(ev) {
        if (this.state.step === "progress") return;
        if (ev.target.classList.contains("ic-modal-overlay")) {
            this.close();
        }
    }

    get isUrlValid() {
        // Every "/" escaped, also inside the classes where JS does not
        // need it: Babel's extractor takes an unescaped one for the end
        // of the literal and missed every translatable string after it.
        return /github\.com[:\/][^\/]+\/[^\/]+/.test(this.state.repoUrl.trim());
    }

    /**
     * Whether the branch field waits for the branches of the URL entered.
     * A branch typed before they arrive was replaced by the default one
     * when they did, and the import took ``main`` (QA walk 6g2).
     * @returns {boolean}
     */
    get branchLocked() {
        return this.state.loadingBranches || this.state.branchesFor !== this.state.repoUrl;
    }

    /** @returns {string} what the branch field says while it waits, or its hint */
    get branchPlaceholder() {
        if (this.state.loadingBranches) return _t("Loading branches…");
        if (this.branchLocked) return _t("Enter the repository URL first");
        return "main";
    }

    onUrlKeydown(ev) {
        if (ev.key === "Enter" && this.isUrlValid) this.doImport();
    }

    async onUrlBlur() {
        if (!this.isUrlValid) return;
        this.state.repoUrl = toHttpsUrl(this.state.repoUrl.trim());
        await this._fetchBranches(this.state.repoUrl);
    }

    async _fetchBranches(url) {
        this.state.loadingBranches = true;
        this.state.branchError = null;
        this.state.branches = [];
        this.state.branch = "";
        try {
            const data = await rpc("/cloud/get_repo_branches", { url });
            this._applyBranches(data);
        } catch {
            this.state.branchError = _t("Could not fetch branches");
        } finally {
            this.state.branchesFor = url;
            this.state.loadingBranches = false;
        }
    }

    /**
     * Fill the branch field from ``/cloud/get_repo_branches``: its list,
     * and ``main``, ``master`` or the first branch as the default.
     * @param {{ok: boolean, branches?: string[], error?: string}} data
     */
    _applyBranches(data) {
        this.state.branches = data.branches || [];
        if (!data.ok && data.error) {
            this.state.branchError = data.error;
        }
        if (this.state.branches.includes("main")) {
            this.state.branch = "main";
        } else if (this.state.branches.includes("master")) {
            this.state.branch = "master";
        } else if (this.state.branches.length) {
            this.state.branch = this.state.branches[0];
        }
    }

    _addMessage(text) {
        this.state.messages.push({ text, status: "done" });
    }

    async doImport() {
        if (!this.isUrlValid || this.state.step === "progress") return;
        this.state.step = "progress";
        this.state.error = null;
        this.state.messages = [];

        this._addMessage(_t("Cloning repository..."));

        try {
            const result = await rpc("/cloud/import_project", {
                url: this.state.repoUrl.trim(),
                branch: this.state.branch || "main",
            });

            if (!result.ok) {
                this.state.error = result.error || _t("Import failed");
                this.state.step = "error";
                return;
            }

            this._addMessage(_t("Detected: %s project").replace('%s', result.repo_type));
            if (result.repo_count) {
                this._addMessage(_t("%s repositories imported").replace('%s', result.repo_count));
            }
            this._addMessage(_t("Project \"%s\" created").replace('%s', result.project_name));
            this._addMessage(_t("Production instance created"));

            this.state.result = result;
            this.state.step = "done";
        } catch (err) {
            this.state.error = err.message || _t("Unexpected error during import");
            this.state.step = "error";
        }
    }

    async goToInstance() {
        const r = this.state.result;
        if (r) {
            await this.close();
            this.env.navigate("instance_detail", {
                project_id: r.project_id,
                instance_id: r.instance_id,
            });
        }
    }

    retry() {
        this.state.step = "input";
        this.state.error = null;
        this.state.messages = [];
    }
}

// Keep backward-compatible alias
export const ImportOdooshModal = ImportProjectModal;
