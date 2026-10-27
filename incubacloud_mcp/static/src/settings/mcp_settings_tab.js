/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { changedSettings, settingsView } from "../connect_agent/connect_agent_format";

/** Settings → Agents: the agents' OAuth issuer and this panel's MCP URL. */
export class McpSettingsTab extends Component {
    static template = "incubacloud_mcp.McpSettingsTab";
    static props = {};

    /** Read the settings; save them with the page's Save button. */
    setup() {
        this.state = useState({
            loading: true,
            managed: false,
            form: { mcp_oauth_issuer: "", mcp_resource: "" },
        });
        // What was read, to save only what changed (null until read).
        this._loaded = null;
        this.env.registerSettingsSave?.("McpSettings", () => this._save());
        onWillUnmount(() => this.env.unregisterSettingsSave?.("McpSettings"));
        onWillStart(() => this.load());
    }

    /** Read the MCP's settings. */
    async load() {
        try {
            const data = await rpc("/cloud/mcp/get_settings", {});
            this.state.form.mcp_oauth_issuer = data.mcp_oauth_issuer || "";
            this.state.form.mcp_resource = data.mcp_resource || "";
            this.state.managed = Boolean(data.managed);
            this._loaded = { ...this.state.form };
        } catch {
            this.env.toast?.error(_t("Failed to load the agents' settings."));
        } finally {
            this.state.loading = false;
        }
    }

    /** Read-only where the platform sets them (see `settingsView`). */
    get view() {
        return settingsView({ managed: this.state.managed });
    }

    /**
     * Save what changed; nothing when read-only, unchanged or unread.
     *
     * @returns {Promise<{ok: boolean, error?: string}>} the save's answer
     */
    async _save() {
        const vals = changedSettings(this._loaded, this.state.form);
        if (this.view.readonly || !Object.keys(vals).length) {
            return { ok: true };
        }
        const result = await rpc("/cloud/mcp/save_settings", { vals });
        if (result?.ok) {
            this._loaded = { ...this.state.form };
        }
        return result;
    }
}
