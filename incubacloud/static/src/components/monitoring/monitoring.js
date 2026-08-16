/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

import { SearchSelect } from "../search_select/search_select";

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
    static components = { SearchSelect };
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
            hosts: [],
            instances: [],
            // Which host/instance the embed is pinned to. Empty means
            // "let Grafana pick", which is what used to happen always.
            host: "",
            instance: "",
        });
        onWillStart(() => this.loadConfig());
    }

    get dashboards() {
        return Monitoring.DASHBOARDS;
    }

    /**
     * True when this panel's own operator edits the observability
     * settings, so pointing them at Settings is actionable advice.
     *
     * Read from the boot config rather than from the route below on
     * purpose: ``enabled`` and the base URL are settings that change
     * while the app is open, so those are re-fetched, whereas who owns
     * them is a property of the deployment and cannot change under a
     * running session.
     */
    get canConfigure() {
        return this.env.features?.observability?.configure !== false;
    }

    get hostOptions() {
        return this.state.hosts.map((h) => h.name);
    }

    get instanceOptions() {
        return this.state.instances.map((i) => i.name);
    }

    /** True while a tab that needs a subject is showing. */
    get needsHost() {
        return this.state.current === "ic-host";
    }

    get needsInstance() {
        return this.state.current === "ic-instance";
    }

    /** Fetch whether monitoring is configured and where Grafana lives. */
    async loadConfig() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const cfg = await rpc("/cloud/monitoring/config", {});
            this.state.enabled = !!cfg.enabled;
            this.state.baseUrl = cfg.grafana_base_url || "";
            this.state.hosts = cfg.hosts || [];
            this.state.instances = cfg.instances || [];
            // Pin the first of each rather than leaving it blank: a blank
            // subject is exactly the silent default this fixes.
            this.state.host = this.state.hosts[0]?.name || "";
            this.state.instance = this.state.instances[0]?.name || "";
        } catch (err) {
            const msg = err?.data?.message ?? err?.message;
            this.state.error = (typeof msg === "string" && msg)
                ? msg
                : "Could not load the monitoring configuration.";
        } finally {
            this.state.loading = false;
        }
    }

    /** The host an instance runs on, so the embed can scope to it. */
    hostOfInstance(name) {
        return this.state.instances.find((i) => i.name === name)?.host || "";
    }

    /**
     * Build the embed URL for a dashboard.
     *
     * ``kiosk`` strips Grafana's own chrome so the panel supplies the
     * navigation, and the theme follows the SPA's so the embed does not
     * flash a light panel inside a dark app.
     *
     * The subject is passed explicitly. ``kiosk`` also hides the row where
     * Grafana would show its own variable pickers, so a dashboard left on
     * its default rendered one arbitrary host with nothing naming it.
     *
     * @param {string} uid  Dashboard uid, one of DASHBOARDS.
     * @param {Object} [opts]
     * @param {boolean} [opts.kiosk=true]  Strip Grafana's own chrome.
     * @returns {string} The URL, or "" when there is nothing to point at.
     */
    embedUrl(uid, { kiosk = true } = {}) {
        if (!this.state.baseUrl) return "";
        const dash = Monitoring.DASHBOARDS.find((d) => d.uid === uid);
        if (!dash) return "";
        const theme =
            document.documentElement.dataset.icTheme === "light"
                ? "light"
                : "dark";
        const base = this.state.baseUrl.replace(/\/+$/, "");
        let url =
            `${base}/d/${dash.uid}/${dash.slug}` +
            `?${kiosk ? "kiosk&" : ""}theme=${theme}`;
        if (uid === "ic-host" && this.state.host) {
            url += `&var-host=${encodeURIComponent(this.state.host)}`;
        } else if (uid === "ic-instance" && this.state.instance) {
            // Both: container names repeat across hosts, so an instance
            // alone can add up series belonging to two different ones.
            const host = this.hostOfInstance(this.state.instance);
            if (host) {
                url += `&var-host=${encodeURIComponent(host)}`;
            }
            url += `&var-instance=${encodeURIComponent(this.state.instance)}`;
        }
        return url;
    }

    /**
     * The same dashboard, addressed for a tab of its own.
     *
     * Needed because the embed cannot ask for a password. Grafana's
     * sign-in is served by the identity provider, which sends
     * ``frame-ancestors 'self'``; the browser therefore refuses to paint
     * that login page inside a frame hosted on another subdomain, and a
     * visitor without a Grafana session yet sees a blank panel with
     * nothing to click. In its own tab the sign-in completes normally,
     * and every later visit finds the cookie and embeds fine.
     *
     * Grafana's chrome is kept here — in a tab of its own it is what
     * makes the page navigable, and it is where the sign-in prompt is.
     *
     * @param {string} uid  Dashboard uid, one of DASHBOARDS.
     * @returns {string} The URL, or "" when there is nothing to point at.
     */
    externalUrl(uid) {
        return this.embedUrl(uid, { kiosk: false });
    }

    get currentUrl() {
        return this.embedUrl(this.state.current);
    }

    get currentExternalUrl() {
        return this.externalUrl(this.state.current);
    }

    select(uid) {
        this.state.current = uid;
    }

    setHost(name) {
        this.state.host = name || "";
    }

    setInstance(name) {
        this.state.instance = name || "";
    }
}
