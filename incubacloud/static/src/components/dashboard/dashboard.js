/** @odoo-module **/

import { Component, useState, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { parseUTC } from "../../utils/dates";

export class Overview extends Component {
    setup() {
        this.state = useState({
            counts: { projects: 0, hosts: 0, instances: 0, alerts: 0 },
            alerts: [],
            recent_jobs: [],
            loading: true,
        });
        this.loadData();

        this._timer = setInterval(() => this.loadData(), 30000);
        onWillUnmount(() => clearInterval(this._timer));
    }

    async loadData() {
        const data = await rpc('/cloud/get_dashboard', {});
        Object.assign(this.state, {
            counts: data.counts,
            alerts: data.alerts,
            recent_jobs: data.recent_jobs,
            loading: false,
        });
    }

    openCreateProject() {
        this.env.navigate('new_project');
    }

    goToHosts() {
        this.env.navigate('hosts');
    }

    goToAlert() {
        this.env.toggleSlideOver('alerts');
    }

    async dismissAlert(alert) {
        await rpc('/cloud/dismiss_alert', { alert_id: alert.id });
        this.env.refreshAlertCount();
        await this.loadData();
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    timeAgo(dateStr) {
        if (!dateStr) return '';
        const diff = Math.floor((Date.now() - parseUTC(dateStr).getTime()) / 1000);
        if (diff < 60) return `${diff}s ago`;
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    }

    jobStateLabel(state) {
        return {
            pending: _t("Pending"), enqueued: _t("Queued"),
            wait_dependencies: _t("Waiting"), started: _t("Running"),
            done: _t("Done"), failed: _t("Failed"), cancelled: _t("Cancelled"),
        }[state] || state;
    }
}

Overview.template = "incubacloud.Overview";
