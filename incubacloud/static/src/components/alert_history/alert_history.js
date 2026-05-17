/** @odoo-module **/

import { Component, useState, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { parseUTC } from "../../utils/dates";
import { IcModal } from "../ic_modal/ic_modal";

export class AlertHistory extends Component {
    static template = "incubacloud.AlertHistory";
    static components = { IcModal };
    static props = {
        embedded: { type: Boolean, optional: true },
    };

    setup() {
        this.env = useEnv();
        this.state = useState({
            alerts: [],
            loading: true,
            stateFilter: "active",
            resolveModal: null,      // { alert, choices: { [pkgName]: chosen_spec } }
            addonConflictModal: null, // { alert }
            expanded: {},            // { [alertId]: bool } — controls payload reveal
        });
        this.loadAlerts();
    }

    /**
     * Toggle the "show details" disclosure for an alert whose payload
     * carries structured detail (e.g. deduped error log groups).
     * @param {number} alertId
     */
    toggleExpanded(alertId) {
        this.state.expanded[alertId] = !this.state.expanded[alertId];
    }

    goBack() {
        const returnUrl = this.env.getAlertReturnUrl?.();
        if (returnUrl) {
            history.pushState(null, "", returnUrl);
            window.dispatchEvent(new PopStateEvent("popstate"));
        } else {
            this.env.navigate("projects");
        }
    }

    async loadAlerts() {
        this.state.loading = true;
        this.state.alerts = await rpc("/cloud/get_alert_history", {
            state_filter: this.state.stateFilter,
        });
        this.state.loading = false;
        this.env.refreshAlertCount();
    }

    setFilter(f) {
        this.state.stateFilter = f;
        this.loadAlerts();
    }

    openJob(jobId) {
        window.open(`/cloud/log/${jobId}`, "_blank");
    }

    goToInstance(alert) {
        if (alert.instance_id && alert.project_id) {
            this.env.navigate("instance_detail", {
                project_id: alert.project_id,
                instance_id: alert.instance_id,
            });
        }
    }

    openResolve(alert) {
        // Pre-fill choices: default to "existing" for all conflicts
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.name] = c.existing;
        }
        this.state.resolveModal = { alert, choices };
    }

    setChoice(pkgName, spec) {
        if (this.state.resolveModal) {
            this.state.resolveModal.choices[pkgName] = spec;
        }
    }

    resolveManually() {
        const { alert } = this.state.resolveModal;
        this.state.resolveModal = null;
        // Do NOT dismiss — the alert stays active until the field is clean.
        // enqueue() will auto-dismiss it when no conflicts remain.
        if (alert.instance_id) {
            this.env.navigate("instance_detail", {
                instance_id: alert.instance_id,
                project_id: alert.project_id,
            });
        }
    }

    async confirmResolve() {
        const { alert, choices } = this.state.resolveModal;
        this.state.resolveModal = null;
        await rpc("/cloud/resolve_pip_conflict", {
            alert_id: alert.id,
            resolutions: choices,
        });
        await this.loadAlerts();
    }

    closeResolveModal() {
        this.state.resolveModal = null;
    }

    openAddonConflict(alert) {
        // Pre-select the first repo for each conflict
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.addon] = (c.repos || [])[0] || '';
        }
        this.state.addonConflictModal = { alert, choices };
    }

    closeAddonConflictModal() {
        this.state.addonConflictModal = null;
    }

    setAddonChoice(addon, repo) {
        if (this.state.addonConflictModal) {
            this.state.addonConflictModal.choices[addon] = repo;
        }
    }

    async confirmResolveAddon() {
        const { alert, choices } = this.state.addonConflictModal;
        this.state.addonConflictModal = null;
        await rpc("/cloud/resolve_addon_conflict", {
            alert_id: alert.id,
            resolutions: choices,
        });
        await this.loadAlerts();
    }

    async dismissAlert(alert) {
        await rpc("/cloud/dismiss_alert", { alert_id: alert.id });
        await this.loadAlerts();
    }

    formatDate(dateStr) {
        if (!dateStr) return "—";
        const d = parseUTC(dateStr);
        return d.toLocaleString([], {
            month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
        });
    }
}
