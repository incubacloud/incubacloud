/** Continuous recovery, in the instance's Backups tab.
 *
 *  Shows whether the instance archives continuously, how far back it can
 *  be restored and how much it may lose at most; managers switch it on or
 *  off, change that objective and restore the instance to a minute of
 *  its window. Restoring is confirmed by typing the instance's name, as
 *  for a backup restore.
 *
 *  The times the server sends are UTC; the panel shows and asks for
 *  local times and converts.
 */
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcModal } from "@incubacloud/components/ic_modal/ic_modal";
import { RlSelect } from "@incubacloud/components/rl_select/rl_select";
import { PILL_CLASSES, ago, formatDate, fromUtc, toLocalInput } from "./pitr_format";

/** How often the card reads the status again while a job of it runs. */
const FOLLOW_MS = 5000;

/** The chip's text per state. "OK" first for a healthy one: "Archiving"
 *  alone read as something still in progress. */
export const STATE_LABELS = {
    off: _t("Off"),
    enabling: _t("Switching on…"),
    ok: _t("OK · Archiving"),
    lagging: _t("Behind"),
    failed: _t("Failing"),
};

/** The recovery objective's label per value (minutes). */
export const OBJECTIVE_LABELS = {
    1: _t("1 minute"),
    5: _t("5 minutes"),
    15: _t("15 minutes"),
    60: _t("1 hour"),
};

/** A datetime-local value (local time) → "YYYY-MM-DD HH:MM:SS" in UTC. */
function localInputToUtc(value) {
    const iso = new Date(value).toISOString();
    return iso.slice(0, 19).replace("T", " ");
}

export class PitrPanel extends Component {
    static template = "incubacloud_pitr.PitrPanel";
    static components = { IcModal, RlSelect };
    static props = {
        instanceId: Number,
        instanceName: { type: String, optional: true },
        environment: { type: String, optional: true },
        canManage: { type: Boolean, optional: true },
        enqueue: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({
            loading: true,
            data: null,
            error: "",
            restoring: false,
            when: "",
            confirmName: "",
            busy: false,
        });
        this.followTimer = null;
        this.alive = true;
        onWillStart(() => this.load());
        onWillUnmount(() => {
            this.alive = false;
            clearTimeout(this.followTimer);
        });
    }

    async load() {
        this.state.loading = true;
        try {
            const data = await rpc("/cloud/pitr/status", {
                instance_id: this.props.instanceId,
            });
            if (data.ok) {
                this.state.data = data;
                this.state.error = "";
            } else {
                this.state.error = data.error || _t("Could not read it.");
            }
        } finally {
            this.state.loading = false;
        }
        this.follow();
    }

    /** Read the status again while a job of this module runs for the
     *  instance, so the card ends where the jobs leave it — switched on,
     *  off or restored — without reloading the page. A read that fails
     *  is tried again on the next tick rather than shown. */
    follow() {
        clearTimeout(this.followTimer);
        if (this.alive && this.state.data?.busy) {
            this.followTimer = setTimeout(
                () => this.load().catch(() => this.follow()),
                FOLLOW_MS,
            );
        }
    }

    /** The chip's text: the state, and for a healthy one that it is OK
     *  — "Archiving" alone read as something in progress. */
    get stateLabel() {
        const data = this.state.data;
        if (data && !data.enabled && data.state !== "off" && data.busy) {
            return _t("Switching off…");
        }
        return STATE_LABELS[data?.state] || "";
    }

    /** The panel's own pill class for the state (``rl-pill``). */
    get pillClass() {
        const data = this.state.data;
        if (data && !data.enabled && data.state !== "off" && data.busy) {
            return "building";
        }
        return PILL_CLASSES[data?.state] || "stopped";
    }

    get windowFrom() {
        return fromUtc(this.state.data?.from);
    }

    get windowTo() {
        return fromUtc(this.state.data?.to);
    }

    get lastArchived() {
        return fromUtc(this.state.data?.last_archived);
    }

    /** When the panel last read the host: "last archived" is only as
     *  fresh as this, and the watch runs every ten minutes. */
    get checkedAt() {
        return fromUtc(this.state.data?.checked);
    }

    formatDate(date) {
        return formatDate(date);
    }

    /** Whole sentences, so each is translated as one. */
    get windowText() {
        return _t("any minute from %s to now", formatDate(this.windowFrom));
    }

    get keptText() {
        return _t("(keeps %s days)", this.state.data?.window_days);
    }

    get cadenceText() {
        return _t("(the watch runs every %s min)", 10);
    }

    get rangeText() {
        return _t("Between %s and now.", formatDate(this.windowFrom));
    }

    ago(date) {
        return ago(date);
    }

    objectiveLabel(minutes) {
        return OBJECTIVE_LABELS[minutes] || _t("%s min", minutes);
    }

    get objectiveOptions() {
        return (this.state.data?.objectives || []).map((o) => ({
            value: o,
            label: this.objectiveLabel(o),
        }));
    }

    async _run(route, params, label) {
        if (this.state.busy) return null;
        this.state.busy = true;
        try {
            const call = () => rpc(route, { instance_id: this.props.instanceId, ...params });
            const data = this.props.enqueue
                ? await this.props.enqueue(call, { openLog: true, errorLabel: label })
                : await call();
            await this.load();
            return data;
        } finally {
            this.state.busy = false;
        }
    }

    enable() {
        return this._run("/cloud/pitr/enable", {}, _t("Could not switch it on"));
    }

    disable() {
        return this._run("/cloud/pitr/disable", {}, _t("Could not switch it off"));
    }

    setObjective(value) {
        if (!value || value === this.state.data?.objective) return null;
        return this._run(
            "/cloud/pitr/set_objective",
            { objective: value },
            _t("Could not change the objective"),
        );
    }

    /** The dialog opens on the present, to the minute: the operator
     *  moves it back to the moment they want, never to a guess. */
    openRestore() {
        this.state.when = toLocalInput(this.windowTo || new Date());
        this.state.confirmName = "";
        this.state.restoring = true;
    }

    closeRestore() {
        this.state.restoring = false;
    }

    get restoreReady() {
        return (
            this.state.when &&
            this.state.confirmName.trim() === (this.props.instanceName || "")
        );
    }

    async restore() {
        if (!this.restoreReady) return;
        const data = await this._run(
            "/cloud/pitr/restore",
            {
                target: localInputToUtc(this.state.when),
                confirm_name: this.state.confirmName.trim(),
            },
            _t("Restore failed"),
        );
        if (data && data.ok !== false) {
            this.state.restoring = false;
        }
    }

    get minInput() {
        return this.windowFrom ? toLocalInput(this.windowFrom) : "";
    }

    get maxInput() {
        return this.windowTo ? toLocalInput(this.windowTo) : "";
    }
}
