import { Component, useState, onWillStart, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

export class BackupBackendsList extends Component {
    static props = {};
    static template = "incubacloud.BackupBackendsList";

    setup() {
        this.env = useEnv();
        this.state = useState({
            loading: true,
            backends: [],
            visible_backends: [],
            error: null,
        });
        this._searchTimer = null;
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.backends = await rpc("/cloud/get_backup_backends", {});
            this.state.visible_backends = this.state.backends;
        } catch {
            this.state.error = _t("Failed to load backup backends.");
        } finally {
            this.state.loading = false;
        }
    }

    onSearchInput(ev) {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
            const q = (ev.target.value || "").toLowerCase().trim();
            this.state.visible_backends = q
                ? this.state.backends.filter(b =>
                    (b.name || "").toLowerCase().includes(q)
                    || (b.backup_dst || "").toLowerCase().includes(q)
                    || (b.s3_bucket || "").toLowerCase().includes(q))
                : this.state.backends;
        }, 300);
    }

    openBackend(id) {
        this.env.navigate("backup_backend_detail", { backend_id: id });
    }

    newBackend() {
        this.env.navigate("new_backup_backend");
    }
}
