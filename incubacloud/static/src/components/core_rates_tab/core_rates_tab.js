/** @odoo-module **/

import { Component, useState, useEnv, onWillStart, onMounted, onWillUnmount, status } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

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
});

export class CoreRatesTab extends Component {
    static template = "incubacloud.CoreRatesTab";
    static props = {};

    setup() {
        this.env = useEnv();
        // Whether the form holds what the server has (see onMounted).
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
        onMounted(() => this._register());
        onWillUnmount(() => this.env.unregisterSettingsSave?.("CoreRates"));
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
            });
            this.loaded = true;
        } catch {
            this.state.loadError = _t("Failed to load rate-limit settings.");
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Join Settings' save registry, once the form holds what the server has.
     *
     * Only while mounted: a retry can answer after its tab was taken down
     * (Settings loading again), and that tab must not stand in for the
     * one now on screen.
     */
    _register() {
        if (this.loaded && status(this) === "mounted") {
            this.env.registerSettingsSave?.("CoreRates", () => this._save(), this.state);
        }
    }

    /** Load again after a failure, and join Settings if it worked. */
    async reload() {
        await this.load();
        this._register();
    }

    onIntInput(field, ev) {
        const raw = parseInt(ev.target.value || 0, 10);
        // Clamp to >=1 here so we can't send a value the operator
        // didn't mean. The backend also clamps negatives to 0 and the
        // model treats 0 as "use documented default" — belt and
        // suspenders.
        this.state.form[field] = Number.isFinite(raw) ? Math.max(1, raw) : 1;
    }

    async _save() {
        return await rpc("/cloud/save_core_rate_limits", {
            vals: {
                rate_limit_webhook_per_min:
                    this.state.form.rate_limit_webhook_per_min,
                rate_limit_terminal_per_min:
                    this.state.form.rate_limit_terminal_per_min,
                rate_limit_terminal_user_per_min:
                    this.state.form.rate_limit_terminal_user_per_min,
                rate_limit_host_console_per_min:
                    this.state.form.rate_limit_host_console_per_min,
                rate_limit_connect_per_min:
                    this.state.form.rate_limit_connect_per_min,
                rate_limit_connect_user_per_min:
                    this.state.form.rate_limit_connect_user_per_min,
                rate_limit_logs_per_min:
                    this.state.form.rate_limit_logs_per_min,
                rate_limit_mail_reads_per_min:
                    this.state.form.rate_limit_mail_reads_per_min,
                rate_limit_log_search_per_min:
                    this.state.form.rate_limit_log_search_per_min,
                rate_limit_github_previews_per_hour:
                    this.state.form.rate_limit_github_previews_per_hour,
                rate_limit_github_imports_per_hour:
                    this.state.form.rate_limit_github_imports_per_hour,
            },
        });
    }
}
