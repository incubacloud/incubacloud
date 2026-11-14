/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { changedSettings, settingsView } from "../connect_agent/connect_agent_format";
import { McpAutoApproval } from "../auto_approval/auto_approval";
import { SettingField, SettingsBlock } from "@incubacloud/components/settings_form/settings_form";
import { useSettingsSave } from "@incubacloud/components/settings_form/use_settings_save";

/** Settings → Agents (MCP): the agents' OAuth issuer and this panel's MCP URL. */
export class McpSettingsTab extends Component {
    static template = "incubacloud_mcp.McpSettingsTab";
    static components = { McpAutoApproval, SettingField, SettingsBlock };
    static props = {};

    /** Read the settings; save them with the page's Save button. */
    setup() {
        this.state = useState({
            loading: true,
            // What failed while loading, shown with Retry instead of the form.
            loadError: null,
            managed: false,
            form: { mcp_oauth_issuer: "", mcp_resource: "" },
        });
        // What was read, to save only what changed (null until read).
        this._loaded = null;
        onWillStart(() => this.load());
        // Registered once mounted, and only if it loaded, as core's Rates
        // tab is: a tab Settings cancelled while it loaded never mounts,
        // and one that failed to load shows the error and Retry instead
        // of a form, joining once a retry loads it. Settings then saves
        // it, and counts it as an unsaved change, only while its form
        // differs from what was read. Read-only, where the platform sets
        // both, it does not join: there is nothing to save.
        this.settingsSave = useSettingsSave({
            key: "McpSettings",
            save: () => this._save(),
            state: this.state,
            ready: () => Boolean(this._loaded) && !this.view.readonly,
        });
    }

    /** Read the MCP's settings. */
    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const data = await rpc("/cloud/mcp/get_settings", {});
            this.state.form.mcp_oauth_issuer = data.mcp_oauth_issuer || "";
            this.state.form.mcp_resource = data.mcp_resource || "";
            this.state.managed = Boolean(data.managed);
            this._loaded = { ...this.state.form };
        } catch {
            this.state.loadError = _t("Failed to load the agents' settings.");
        } finally {
            this.state.loading = false;
        }
    }

    /** Load again after a failure, and join Settings if it worked. */
    async reload() {
        await this.load();
        this.settingsSave.register();
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
        // What is sent, not what the form holds once the answer is in, is
        // what gets saved: an edit made while the request is out stays
        // unsaved, and the next Save sends it.
        const sent = { ...this.state.form };
        const result = await rpc("/cloud/mcp/save_settings", { vals });
        if (result?.ok) {
            this._loaded = sent;
        }
        return result;
    }
}
