/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

import { SearchSelect } from "../search_select/search_select";
import { FleetBoard } from "../metrics/fleet_board";
import { HostBoard } from "../metrics/host_board";

/**
 * Monitoring: the fleet and each server, drawn by the panel from the
 * metrics central (no frame of another tool since 12-nov-2026).
 *
 * Two views. Fleet: every account's servers on the panel that deployed
 * the central, the panel's own anywhere else (the server decides).
 * Hosts: one server's board, the same one as its Metrics tab, picked
 * among the hosts the caller can read.
 *
 * The boards say themselves when observability is off, has no central
 * or the central fails; this page only adds the case of a fleet with
 * no server at all.
 */
export class Monitoring extends Component {
    static template = "incubacloud.Monitoring";
    static components = { SearchSelect, FleetBoard, HostBoard };
    static props = {};

    setup() {
        this.views = [
            { key: "fleet", label: _t("Fleet") },
            { key: "hosts", label: _t("Hosts") },
        ];
        this.state = useState({
            loading: true,
            error: "",
            view: "fleet",
            hosts: [],
            // The host the Hosts view shows, as a string id ("" for none).
            host: "",
        });
        onWillStart(() => this.loadConfig());
    }

    /**
     * True when this panel's own operator edits the observability
     * settings, so pointing them at Settings is actionable advice.
     *
     * Read from the boot config: who owns the settings is a property of
     * the deployment and cannot change under a running session.
     */
    get canConfigure() {
        return this.env.features?.observability?.configure !== false;
    }

    /** True when the fleet has at least one server that can report. */
    get hasHosts() {
        return this.state.hosts.length > 0;
    }

    /** Options for the host picker: the name shown, the id sent. */
    get hostOptions() {
        return this.state.hosts.map((h) => ({ value: String(h.id), label: h.name }));
    }

    /** The id of the host the Hosts view shows, or 0. */
    get hostId() {
        return Number(this.state.host) || 0;
    }

    /** Fetch the hosts the caller can read. */
    async loadConfig() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const cfg = await rpc("/cloud/monitoring/config", {});
            this.state.hosts = cfg.hosts || [];
            // Pin the first one: a blank subject would show nothing.
            this.state.host = this.state.hosts[0] ? String(this.state.hosts[0].id) : "";
        } catch (err) {
            const msg = err?.data?.message ?? err?.message;
            this.state.error = (typeof msg === "string" && msg)
                ? msg
                : _t("Could not load the monitoring configuration.");
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Switch between the fleet and one server.
     *
     * @param {string} key ``fleet`` or ``hosts``
     */
    select(key) {
        this.state.view = key;
    }

    /**
     * Show a server in the Hosts view.
     *
     * @param {string} value the host's id, as the picker gives it
     */
    setHost(value) {
        this.state.host = value || "";
    }

    /**
     * Open a server of the fleet in the Hosts view.
     *
     * @param {number} hostId
     */
    openHost(hostId) {
        if (this.state.hosts.some((h) => h.id === hostId)) {
            this.state.host = String(hostId);
            this.state.view = "hosts";
        }
    }
}
