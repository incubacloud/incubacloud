/** @odoo-module **/

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { parseUTC } from "../../utils/dates";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";
import { useDebouncedBus } from "../../utils/use_debounced_bus";

export class Overview extends Component {
    setup() {
        this.state = useState({
            counts: { projects: 0, hosts: 0, instances: 0, alerts: 0 },
            alerts: [],
            recent_jobs: [],
            loading: true,
        });
        this.loadData();

        // Both channels invalidate the overview:
        // ``cloud_overview`` → alerts state / counts changed.
        // ``cloud_jobs``     → recent_jobs list changed (state transition).
        // The single debouncer collapses the common "job completes +
        // creates alert" case into one loadData() instead of two
        // overlapping RPCs.
        this._busService = useService("bus_service");
        const triggerReload = useDebouncedBus(() => {
            if (!this._destroyed) this.loadData();
        });
        this._onInvalidate = () => triggerReload();

        onMounted(() => {
            this._busService.subscribe("cloud_overview", this._onInvalidate);
            this._busService.subscribe("cloud_jobs", this._onInvalidate);
            this._busService.start();
        });
        onWillUnmount(() => {
            this._destroyed = true;
            this._busService.unsubscribe("cloud_overview", this._onInvalidate);
            this._busService.unsubscribe("cloud_jobs", this._onInvalidate);
        });
        useVisibilityRefresh(() => {
            if (!this._destroyed) this.loadData();
        });
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
