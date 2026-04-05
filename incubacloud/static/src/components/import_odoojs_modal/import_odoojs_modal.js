import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { SearchSelect } from "../search_select/search_select";

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
        return /github\.com[:/][^/]+\/[^/]+/.test(this.state.repoUrl.trim());
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
        } catch {
            this.state.branchError = _t("Could not fetch branches");
        } finally {
            this.state.loadingBranches = false;
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
