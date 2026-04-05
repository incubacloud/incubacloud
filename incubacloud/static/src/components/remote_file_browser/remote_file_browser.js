import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

export class RemoteFileBrowser extends Component {
    static template = "incubacloud.RemoteFileBrowser";
    static props = {
        hostId: Number,
        onSelect: Function,
        onCancel: Function,
    };

    setup() {
        this.state = useState({
            step: "browse",  // browse | confirm | importing | done | error
            loading: true,
            currentPath: "",
            parentPath: "",
            entries: [],
            isDoodba: false,
            copierInfo: {},
            alreadyImported: false,
            error: null,
            // Import result
            result: null,
            messages: [],
        });
        onWillStart(() => this.navigate("~"));
    }

    async navigate(path) {
        this.state.loading = true;
        this.state.error = null;
        try {
            const data = await rpc("/cloud/browse_host_dir", {
                host_id: this.props.hostId,
                path,
            });
            if (!data.ok) {
                this.state.error = data.error;
                this.state.loading = false;
                return;
            }
            this.state.currentPath = data.current_path;
            this.state.parentPath = data.parent_path;
            this.state.entries = data.entries;
            this.state.isDoodba = data.is_doodba;
            this.state.copierInfo = data.copier_info || {};
            this.state.alreadyImported = data.already_imported;
        } catch (err) {
            this.state.error = err.message || _t("Failed to browse directory");
        } finally {
            this.state.loading = false;
        }
    }

    get breadcrumbs() {
        const parts = this.state.currentPath.split("/").filter(Boolean);
        const crumbs = [{ name: "/", path: "/" }];
        let acc = "";
        for (const p of parts) {
            acc += "/" + p;
            crumbs.push({ name: p, path: acc });
        }
        return crumbs;
    }

    onClickDir(entry) {
        this.navigate(this.state.currentPath + "/" + entry.name);
    }

    onClickBreadcrumb(path) {
        this.navigate(path);
    }

    goUp() {
        if (this.state.parentPath) {
            this.navigate(this.state.parentPath);
        }
    }

    selectCurrent() {
        this.state.step = "confirm";
    }

    backToBrowse() {
        this.state.step = "browse";
    }

    _addMessage(text) {
        this.state.messages.push({ text, status: "done" });
    }

    async doImport() {
        this.state.step = "importing";
        this.state.messages = [];
        this._addMessage(_t("Reading instance configuration..."));

        try {
            const result = await rpc("/cloud/import_host_instance", {
                host_id: this.props.hostId,
                path: this.state.currentPath,
            });

            if (!result.ok) {
                this.state.error = result.error;
                this.state.step = "error";
                return;
            }

            this._addMessage(_t("Project \"%s\" ready").replace('%s', result.project_name));
            this._addMessage(_t("Instance \"%s\" imported").replace('%s', result.instance_name));
            this._addMessage(
                result.is_running ? _t("Status: running") : _t("Status: stopped"),
            );

            this.state.result = result;
            this.state.step = "done";
        } catch (err) {
            this.state.error = err.message || _t("Import failed");
            this.state.step = "error";
        }
    }

    goToInstance() {
        const r = this.state.result;
        if (r) {
            this.props.onSelect(r);
        }
    }

    retry() {
        this.state.step = "browse";
        this.state.error = null;
        this.state.messages = [];
    }
}
