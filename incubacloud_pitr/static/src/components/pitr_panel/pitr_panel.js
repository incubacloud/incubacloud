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

/** How often the card reads the status again while a job of it runs. */
const FOLLOW_MS = 5000;

/** "2026-10-18 22:10:29" (UTC) → Date. */
function fromUtc(value) {
    return value ? new Date(value.replace(" ", "T") + "Z") : null;
}

/** Date → the "YYYY-MM-DDTHH:MM" a datetime-local input takes (local). */
function toLocalInput(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return (
        `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
        `T${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
}

/** A datetime-local value (local time) → "YYYY-MM-DD HH:MM:SS" in UTC. */
function localInputToUtc(value) {
    const iso = new Date(value).toISOString();
    return iso.slice(0, 19).replace("T", " ");
}

export class PitrPanel extends Component {
    static template = "incubacloud_pitr.PitrPanel";
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

    get stateLabel() {
        const data = this.state.data;
        if (data && !data.enabled && data.state !== "off" && data.busy) {
            return _t("Switching off…");
        }
        return {
            off: _t("Off"),
            enabling: _t("Switching on…"),
            ok: _t("Archiving"),
            lagging: _t("Behind"),
            failed: _t("Failing"),
        }[this.state.data?.state] || "";
    }

    get windowFrom() {
        return fromUtc(this.state.data?.from);
    }

    get windowTo() {
        return fromUtc(this.state.data?.to);
    }

    get minutesSinceArchived() {
        const last = fromUtc(this.state.data?.last_archived);
        return last ? Math.max(0, Math.round((Date.now() - last) / 60000)) : null;
    }

    formatDate(date) {
        return date ? date.toLocaleString() : "";
    }

    objectiveLabel(minutes) {
        return minutes === "60" ? _t("1 hour") : _t("%s min", minutes);
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

    setObjective(ev) {
        return this._run(
            "/cloud/pitr/set_objective",
            { objective: ev.target.value },
            _t("Could not change the objective"),
        );
    }

    openRestore() {
        const to = this.windowTo || new Date();
        this.state.when = toLocalInput(new Date(to.getTime() - 15 * 60000));
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
