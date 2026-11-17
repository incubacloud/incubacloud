import { Component, onWillStart, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { chartLocale } from "../metrics/time_series_chart";

const { DateTime } = luxon;

/** Whose a sign-in looks, in words: the Security view's, said when asked. */
const ATTRIBUTION_WORDS = {
    panel: () => _t("the panel"),
    panel_elsewhere: () => _t("the panel's key from another address"),
    foreign_key: () => _t("another key"),
    password: () => _t("a password"),
    other_user: () => _t("another user"),
};

/**
 * A server's Access tab: the account and key the panel signs in with, the
 * last sign-ins its security logs show, and what deleting the server from
 * the panel takes off it (``/cloud/host/access``).
 */
export class HostAccess extends Component {
    static template = "incubacloud.HostAccess";
    static props = {
        hostId: Number,
        onOpenSecurity: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({ loading: true, error: "", access: null });
        onWillStart(() => this.load());
    }

    /** Read the tab's facts. */
    async load() {
        this.state.loading = true;
        try {
            const answer = await rpc("/cloud/host/access", { host_id: this.props.hostId });
            if (answer.ok) {
                this.state.access = answer;
                this.state.error = "";
            } else {
                this.state.error = answer.error || _t("The server's access could not be read.");
            }
        } catch (_e) {
            this.state.error = _t("The server's access could not be read.");
        }
        this.state.loading = false;
    }

    /** The sign-ins part of the answer. */
    get signIns() {
        return this.state.access?.sign_ins || { state: "error" };
    }

    /**
     * When a sign-in happened, in the reader's language.
     *
     * @param {string} iso
     * @returns {string}
     */
    fmtAt(iso) {
        if (!iso) {
            return "—";
        }
        return DateTime.fromISO(iso).setLocale(chartLocale() || "en").toLocaleString(DateTime.DATETIME_SHORT);
    }

    /**
     * Whose a sign-in looks, in words: the Security view's.
     *
     * @param {string} code
     * @returns {string}
     */
    attributionText(code) {
        const say = ATTRIBUTION_WORDS[code];
        return say ? say() : code || "—";
    }

    /**
     * Whether a sign-in was not the panel's from where it connects.
     *
     * @param {string} code
     * @returns {boolean}
     */
    attributionBad(code) {
        return ["panel_elsewhere", "foreign_key", "password", "other_user"].includes(code);
    }

    /** Open the Security view of the Metrics tab. */
    openSecurity() {
        this.props.onOpenSecurity?.();
    }
}
