import { Component, useState, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { parseUTC } from "../../utils/dates";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { useDebouncedBus } from "../../utils/use_debounced_bus";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";

const ACTIVE_STATES = ["pending", "enqueued", "wait_dependencies", "started", "blocked"];

export class JobDrawer extends Component {
    static template = "incubacloud.JobDrawer";
    static components = { IcConfirmDialog };

    setup() {
        this.state = useState({
            jobs: [],
            confirmDialog: null,
        });

        this.orm = useService("orm");
        this.busService = useService("bus_service");
        // Debounce ``fetchJob``: chunk-flush storms during a running
        // build otherwise fire dozens of ``load_jobs(id)`` RPCs per
        // second. ``last-write-wins`` means if two different jobs
        // transition within 300 ms we only fetch the latest one; the
        // visibility refresh + the next event's window absorb the
        // miss, and ``loadInitialJobs`` re-hydrates the full list on
        // tab focus as a safety net.
        const triggerFetch = useDebouncedBus((jobId) => {
            if (jobId != null) this.fetchJob(jobId);
        });
        this._boundOnJobUpdate = (payload) => triggerFetch(payload.id);
        this.busService.subscribe("cloud_jobs", this._boundOnJobUpdate);
        this.busService.start();
        this.loadInitialJobs();
        onWillUnmount(() => {
            this.busService.unsubscribe("cloud_jobs", this._boundOnJobUpdate);
        });
        useVisibilityRefresh(() => this.loadInitialJobs());
    }

    // ───────────── Getters ─────────────

    get hasJobs() {
        return this.state.jobs.length > 0;
    }

    get hasRunningJobs() {
        return this.state.jobs.some(j => ACTIVE_STATES.includes(j.state));
    }

    get runningCount() {
        return this.state.jobs.filter(j => ACTIVE_STATES.includes(j.state)).length;
    }

    // ───────────── Data loading ─────────────

    async loadInitialJobs() {
        const result = await this.orm.call(
            "cloud.job",
            "load_jobs",
        );

        this.state.jobs = [
            ...result.active,
            ...result.recent,
        ];

    }

    async fetchJob(jobId) {
        const [job] = await this.orm.call(
            "cloud.job",
            "load_jobs",
            [jobId],
        );

        const idx = this.state.jobs.findIndex(j => j.id === job.id);
        if (idx >= 0) {
            this.state.jobs[idx] = job;
        } else {
            this.state.jobs.unshift(job);
        }
    }

    // ───────────── UI actions ─────────────

    goToHistory() {
        this.env.navigate("jobs_history");
    }

    openLog(job) {
        window.open(`/cloud/log/${job.id}`, '_blank');
    }

    async cancelJob(job) {
        const ok = await this._confirm({
            title: _t("Cancel job"),
            message: _t('Cancel "%s"? This cannot be undone.').replace(
                "%s", job.name,
            ),
            confirmLabel: _t("Cancel job"),
            isDanger: true,
        });
        if (ok) {
            this.orm.call("cloud.job", "cancel_job", [job.id]);
        }
    }

    _confirm({ title, message, confirmLabel = _t("Confirm"), isDanger = false }) {
        return new Promise((resolve) => {
            this.state.confirmDialog = {
                title, message, confirmLabel, isDanger,
                onConfirm: () => { this.state.confirmDialog = null; resolve(true); },
                onCancel:  () => { this.state.confirmDialog = null; resolve(false); },
            };
        });
    }

    async retryJob(job) {
        const newJobId = await this.orm.call("cloud.job", "retry_job", [job.id]);
        if (newJobId) window.open(`/cloud/log/${newJobId}`, "_blank");
    }

    jobClass(job) {
        return {
            pending: "job-pending",
            enqueued: "job-pending",
            wait_dependencies: "job-pending",
            started: "job-running",
            done: "job-done",
            failed: "job-failed",
            cancelled: "job-cancelled",
            blocked: "job-blocked",
        }[job.state] || "";
    }

    stateName(job) {
        return {
            pending: "Pending",
            enqueued: "Queued",
            wait_dependencies: "Waiting",
            started: "Running",
            done: "Done",
            failed: "Failed",
            cancelled: "Cancelled",
            blocked: "Blocked",
        }[job.state] || job.state;
    }

    formatDate(dateStr) {
        if (!dateStr) return "";
        const d = parseUTC(dateStr);
        return d.toLocaleString([], {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    }
}
