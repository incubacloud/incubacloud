/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
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
        this.state = useState({ loading: true, loaded: false, value: MCP_RATE_DEFAULT });
        this.env.registerSettingsSave?.("McpRates", () => this._save());
        onWillUnmount(() => this.env.unregisterSettingsSave?.("McpRates"));
        onWillStart(() => this.load());
    }

    /** Read the limit. */
    async load() {
        try {
            const data = await rpc("/cloud/mcp/get_settings", {});
            this.state.value = Number(data.rate_limit_mcp_user_per_min || MCP_RATE_DEFAULT);
            this.state.loaded = true;
        } catch {
            this.env.toast?.error(_t("Failed to load the agents' limit."));
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Keep a positive whole number, as the core limits do.
     *
     * @param {Event} ev the input's change event
     */
    onInput(ev) {
        const raw = parseInt(ev.target.value || 0, 10);
        this.state.value = Number.isFinite(raw) ? Math.max(1, raw) : 1;
    }

    /**
     * Save with the page's Save button; never over a limit that was not read.
     *
     * @returns {Promise<{ok: boolean, error?: string}>} the save's answer
     */
    async _save() {
        if (!this.state.loaded) {
            return { ok: true };
        }
        return await rpc("/cloud/mcp/save_settings", {
            vals: { rate_limit_mcp_user_per_min: this.state.value },
        });
    }
}
