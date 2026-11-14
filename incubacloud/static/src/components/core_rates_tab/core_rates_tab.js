/** @odoo-module **/

import { Component, useState, useEnv, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { SettingAdvanced, SettingField, SettingsBlock } from "../settings_form/settings_form";
import { useSettingsSave } from "../settings_form/use_settings_save";

export const CORE_RATE_DEFAULTS = Object.freeze({
    rate_limit_webhook_per_min: 300,
    rate_limit_terminal_per_min: 30,
    rate_limit_terminal_user_per_min: 10,
    rate_limit_host_console_per_min: 3,
    rate_limit_connect_per_min: 20,
    rate_limit_connect_user_per_min: 10,
    rate_limit_logs_per_min: 60,
    rate_limit_mail_reads_per_min: 30,
    rate_limit_log_search_per_min: 6,
    rate_limit_github_previews_per_hour: 10,
    rate_limit_github_imports_per_hour: 5,
    rate_limit_git_reads_per_hour: 120,
});

export class CoreRatesTab extends Component {
    static template = "incubacloud.CoreRatesTab";
    static components = { SettingAdvanced, SettingField, SettingsBlock };
    static props = {};

    setup() {
        this.env = useEnv();
        // Whether the form holds what the server has (see settingsSave).
        this.loaded = false;
        this.state = useState({
            loading: true,
            // What failed while loading, shown with Retry instead of the form.
            loadError: null,
            form: { ...CORE_RATE_DEFAULTS },
        });

        onWillStart(() => this.load());
        // Registered once mounted, and only if it loaded. A tab Settings'
        // re-render cancelled while it loaded never mounts, so it cannot
        // stand in for the live one; and a tab that failed to load shows
        // the error and Retry instead of a form of defaults, and joins
        // once a retry loads it. Settings saves the tab, and warns before
        // leaving, only while the form differs from what it loaded.
        this.settingsSave = useSettingsSave({
            key: "CoreRates",
            save: () => this._save(),
            state: this.state,
            ready: () => this.loaded,
        });
    }

    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const d = await rpc("/cloud/get_core_rate_limits", {});
            Object.assign(this.state.form, {
                rate_limit_webhook_per_min: Number(
                    d.rate_limit_webhook_per_min || 300,
                ),
                rate_limit_terminal_per_min: Number(
                    d.rate_limit_terminal_per_min || 30,
                ),
                rate_limit_terminal_user_per_min: Number(
                    d.rate_limit_terminal_user_per_min || 10,
                ),
                rate_limit_host_console_per_min: Number(
                    d.rate_limit_host_console_per_min || 3,
                ),
                rate_limit_connect_per_min: Number(
                    d.rate_limit_connect_per_min || 20,
                ),
                rate_limit_connect_user_per_min: Number(
                    d.rate_limit_connect_user_per_min || 10,
                ),
                rate_limit_logs_per_min: Number(
                    d.rate_limit_logs_per_min || 60,
                ),
                rate_limit_mail_reads_per_min: Number(
                    d.rate_limit_mail_reads_per_min || 30,
                ),
                rate_limit_log_search_per_min: Number(
                    d.rate_limit_log_search_per_min || 6,
                ),
                rate_limit_github_previews_per_hour: Number(
                    d.rate_limit_github_previews_per_hour || 10,
                ),
                rate_limit_github_imports_per_hour: Number(
                    d.rate_limit_github_imports_per_hour || 5,
                ),
                rate_limit_git_reads_per_hour: Number(
                    d.rate_limit_git_reads_per_hour || 120,
                ),
            });
            this.loaded = true;
        } catch {
            this.state.loadError = _t("Failed to load rate-limit settings.");
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
     * Save the limits changed; Settings checked each against its
     * declaration (a whole number, 1 or more) first.
     *
     * @returns {Promise<object>} the server's answer
     */
    async _save() {
        const form = this.state.form;
        return await rpc("/cloud/save_core_rate_limits", {
            vals: this.env.settingsForm?.changes("CoreRates", form) ?? { ...form },
        });
    }
}
