import { Component, useState, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { parseUTC } from "../../utils/dates";

const ACTIVE_STATES = ["pending", "enqueued", "wait_dependencies", "started", "blocked"];

export class JobDrawer extends Component {
    static template = "incubacloud.JobDrawer";

    setup() {
        this.state = useState({
            jobs: [],
            confirmDialog: null,
        });

        this.orm = useService("orm");
        this.busService = useService("bus_service");
        this._boundOnJobUpdate = this.onJobUpdate.bind(this);
        this.busService.subscribe("cloud_jobs", this._boundOnJobUpdate);
        this.busService.start();
        this.loadInitialJobs();
        onWillUnmount(() => {
            this.busService.unsubscribe("cloud_jobs", this._boundOnJobUpdate);
        });
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

    // ───────────── Bus handling ─────────────

    onJobUpdate(payload) {
        this.fetchJob(payload.id);
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
