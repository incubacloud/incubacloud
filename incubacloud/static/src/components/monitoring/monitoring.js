/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

/**
 * Monitoring — the operator view of the metrics stack (Fase 4 / A10).
 *
 * Grafana is embedded rather than reimplemented: rebuilding time-series
 * charts inside the SPA would be weeks of work to end up with something
 * worse than the tool the dashboards are already provisioned for.
 *
 * Auth: Grafana runs anonymous **behind the panel's proxy**, and access
 * is gated by the panel itself (this route only renders for users who can
 * manage hosts). A second Grafana session would duplicate the operator's
 * login for no isolation gain — per-tenant isolation is a different
 * problem (the deferred tenant-facing view) that needs query-level
 * filtering, not a login prompt.
 *
 * The component degrades honestly: when observability is off or the base
 * URL is unset it says so and points at Settings, instead of rendering a
 * broken iframe.
 */
export class Monitoring extends Component {
    static template = "incubacloud.Monitoring";
    static props = {};

    // Dashboards provisioned by the central playbook. ``uid`` must match
    // the JSON files shipped in ansible/files/dashboards/.
    static DASHBOARDS = [
        { uid: "ic-fleet", slug: "incubacloud-fleet", label: "Fleet" },
        { uid: "ic-host", slug: "incubacloud-host", label: "Hosts" },
        { uid: "ic-instance", slug: "incubacloud-instance", label: "Instances" },
    ];

    setup() {
        this.state = useState({
            loading: true,
            error: "",
            enabled: false,
            baseUrl: "",
            current: Monitoring.DASHBOARDS[0].uid,
        });
        onWillStart(() => this.loadConfig());
    }

    get dashboards() {
        return Monitoring.DASHBOARDS;
    }

    /** Fetch whether monitoring is configured and where Grafana lives. */
    async loadConfig() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const cfg = await rpc("/cloud/monitoring/config", {});
            this.state.enabled = !!cfg.enabled;
            this.state.baseUrl = cfg.grafana_base_url || "";
        } catch (err) {
            const msg = err?.data?.message ?? err?.message;
            this.state.error = (typeof msg === "string" && msg)
                ? msg
                : "Could not load the monitoring configuration.";
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Build the embed URL for a dashboard.
     *
     * ``kiosk`` strips Grafana's own chrome so the panel supplies the
     * navigation, and the theme follows the SPA's so the embed does not
     * flash a light panel inside a dark app.
     */
    embedUrl(uid) {
        if (!this.state.baseUrl) return "";
        const dash = Monitoring.DASHBOARDS.find((d) => d.uid === uid);
        if (!dash) return "";
        const theme =
            document.documentElement.dataset.icTheme === "light"
                ? "light"
                : "dark";
        const base = this.state.baseUrl.replace(/\/+$/, "");
        return `${base}/d/${dash.uid}/${dash.slug}?kiosk&theme=${theme}`;
    }

    get currentUrl() {
        return this.embedUrl(this.state.current);
    }

    select(uid) {
        this.state.current = uid;
    }
}
