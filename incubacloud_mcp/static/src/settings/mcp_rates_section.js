/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { SettingField, SettingsBlock } from "@incubacloud/components/settings_form/settings_form";
import { useSettingsSave } from "@incubacloud/components/settings_form/use_settings_save";

/** Calls per user and minute when the setting is empty, as the server says. */
export const MCP_RATE_DEFAULT = 120;

/** Settings → Rate limits: the agents' calls per user and minute. */
export class McpRatesSection extends Component {
    static template = "incubacloud_mcp.McpRatesSection";
    static components = { SettingField, SettingsBlock };
    static props = {};

    /** Read the limit; save it with the page's Save button. */
    setup() {
        this.state = useState({
            loading: true,
            loaded: false,
            // What failed while loading, shown with Retry instead of the field.
            loadError: null,
            // A form, as Settings compares it to what was read (see settingsSave).
            form: { rate_limit_mcp_user_per_min: MCP_RATE_DEFAULT },
        });
        onWillStart(() => this.load());
        // Registered once mounted, and only if it loaded, as core's Rates
        // tab is: a section Settings cancelled while it loaded never
        // mounts, and one that failed to load shows the error and Retry
        // instead of the field, joining once a retry loads it. Settings
        // then saves it, and counts it as an unsaved change, only while
        // the limit differs from what was read.
        this.settingsSave = useSettingsSave({
            key: "McpRates",
            save: () => this._save(),
            state: this.state,
            ready: () => this.state.loaded,
        });
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

    /** Load again after a failure, and join Settings if it worked. */
    async reload() {
        await this.load();
        this.settingsSave.register();
    }

    /**
     * Save with the page's Save button. Never over a limit that was not
     * read: the section registers only once it loaded (see settingsSave).
     *
     * @returns {Promise<{ok: boolean, error?: string}>} the save's answer
     */
    async _save() {
        return await rpc("/cloud/mcp/save_settings", {
            vals: { rate_limit_mcp_user_per_min: this.state.form.rate_limit_mcp_user_per_min },
        });
    }
}
