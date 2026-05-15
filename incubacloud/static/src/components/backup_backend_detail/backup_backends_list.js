import { Component, useState, onWillStart, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { TruncationBanner } from "../truncation_banner/truncation_banner";

export class BackupBackendsList extends Component {
    static props = {};
    static template = "incubacloud.BackupBackendsList";
    static components = { TruncationBanner };

    setup() {
        this.env = useEnv();
        this.state = useState({
            loading: true,
            backends: [],
            visible_backends: [],
            error: null,
            truncated: false,
            total: 0,
            limit: 200,
        });
        this._searchTimer = null;
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const data = await rpc("/cloud/get_backup_backends", {});
            this.state.backends = data.items || [];
            this.state.visible_backends = this.state.backends;
            this.state.truncated = !!data.truncated;
            this.state.total = data.total || this.state.backends.length;
            this.state.limit = data.limit || 200;
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

    usagePctOf(b) {
        const q = parseFloat(b.quota_gb) || 0;
        if (q <= 0) return 0;
        return Math.min(100, ((b.last_measured_gb || 0) / q) * 100);
    }

    // List rows don't carry per-backend threshold or the settings default,
    // so the bar uses a fixed 80% threshold for colour. The detail page
    // shows the precise per-backend threshold once opened.
    usageBandOf(b) {
        const pct = this.usagePctOf(b);
        if (pct >= 80) return "danger";
        if (pct >= 60) return "warning";
        return "ok";
    }
}
