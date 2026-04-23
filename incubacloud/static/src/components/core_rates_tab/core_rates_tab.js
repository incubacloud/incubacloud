/** @odoo-module **/

import { Component, useState, useEnv, onWillStart, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

export class CoreRatesTab extends Component {
    static template = "incubacloud.CoreRatesTab";
    static props = {};

    setup() {
        this.env = useEnv();
        this.state = useState({
            loading: true,
            form: {
                rate_limit_webhook_per_min: 300,
                rate_limit_terminal_per_min: 30,
                rate_limit_terminal_user_per_min: 10,
            },
        });

        this.env.registerSettingsSave?.("CoreRates", () => this._save());
        onWillUnmount(() => this.env.unregisterSettingsSave?.("CoreRates"));

        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
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
            });
        } catch {
            this.env.toast?.error(_t("Failed to load rate-limit settings."));
        } finally {
            this.state.loading = false;
        }
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
            },
        });
    }
}
