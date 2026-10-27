/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, status, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

/** Calls per user and minute when the setting is empty, as the server says. */
export const MCP_RATE_DEFAULT = 120;

/** Settings → Rates: the agents' calls per user and minute. */
export class McpRatesSection extends Component {
    static template = "incubacloud_mcp.McpRatesSection";
    static props = {};

    /** Read the limit; save it with the page's Save button. */
    setup() {
        this.state = useState({
            loading: true,
            loaded: false,
            // What failed while loading, shown with Retry instead of the field.
            loadError: null,
            // A form, as Settings compares it to what was read (see _register).
            form: { rate_limit_mcp_user_per_min: MCP_RATE_DEFAULT },
        });
        onWillStart(() => this.load());
        // Registered once mounted, and only if it loaded, as core's Rates
        // tab is: a section Settings cancelled while it loaded never
        // mounts, and one that failed to load shows the error and Retry
        // instead of the field, joining once a retry loads it.
        onMounted(() => this._register());
        onWillUnmount(() => this.env.unregisterSettingsSave?.("McpRates"));
    }

    /** Read the limit. */
    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const data = await rpc("/cloud/mcp/get_settings", {});
            this.state.form.rate_limit_mcp_user_per_min = Number(
                data.rate_limit_mcp_user_per_min || MCP_RATE_DEFAULT,
            );
            this.state.loaded = true;
        } catch {
            this.state.loadError = _t("Failed to load the agents' limit.");
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Join Settings' save registry, once the field holds what the server has.
     *
     * Only while mounted: a retry can answer after its section was taken
     * down (Settings loading again), and that section must not stand in
     * for the one now on screen. Settings then saves it, and counts it as
     * an unsaved change, only while the limit differs from what was read.
     */
    _register() {
        if (this.state.loaded && status(this) === "mounted") {
            this.env.registerSettingsSave?.("McpRates", () => this._save(), this.state);
        }
    }

    /** Load again after a failure, and join Settings if it worked. */
    async reload() {
        await this.load();
        this._register();
    }

    /**
     * Keep a positive whole number, as the core limits do.
     *
     * @param {Event} ev the input's change event
     */
    onInput(ev) {
        const raw = parseInt(ev.target.value || 0, 10);
        this.state.form.rate_limit_mcp_user_per_min = Number.isFinite(raw) ? Math.max(1, raw) : 1;
    }

    /**
     * Save with the page's Save button. Never over a limit that was not
     * read: the section registers only once it loaded (see _register).
     *
     * @returns {Promise<{ok: boolean, error?: string}>} the save's answer
     */
    async _save() {
        return await rpc("/cloud/mcp/save_settings", {
            vals: { rate_limit_mcp_user_per_min: this.state.form.rate_limit_mcp_user_per_min },
        });
    }
}
