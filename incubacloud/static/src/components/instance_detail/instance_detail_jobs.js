/** @odoo-module **/

import {_t} from "@web/core/l10n/translation";
import {ACTIVE_STATES, announceJob, cancelPrompt, jobStateText} from "../../utils/job_queue";

/**
 * Job timeline: icons, state classes, cancel/retry and paging.
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const JobsMixin = (Base) =>
  class extends Base {
    isActiveJob(state) {
      return ACTIVE_STATES.includes(state);
    }

    /**
     * Text of a job's state pill (``job_queue.jobStateText``).
     *
     * @param {{state: string, queue_position?: number}} job
     * @returns {string}
     */
    jobStateText(job) {
      return jobStateText(job);
    }

    /**
     * Whether a job waits on an alert someone has to act on: blocked on
     * one, or queued behind a failure that holds the instance's queue
     * (``queue_held``) until it is retried or its alert dismissed. Both
     * get the Resolve button, which opens the alerts.
     *
     * @param {{state: string, queue_held?: boolean}} job
     * @returns {boolean}
     */
    waitsOnAlert(job) {
      return job.state === "blocked" || Boolean(job.queue_held);
    }

    jobIcon(code) {
      const map = {
        deploy_instance: "fa-rocket",
        rebuild_instance: "fa-wrench",
        restart_instance: "fa-refresh",
        stop_instance: "fa-stop",
        start_instance: "fa-play",
        export_instance: "fa-download",
        delete_instance: "fa-trash-o",
        restore_instance: "fa-database",
      };
      return map[code] || "fa-cog";
    }

    jobStateClass(state) {
      return (
        {
          done: "jst-done",
          failed: "jst-failed",
          cancelled: "jst-cancelled",
          started: "jst-running",
          pending: "jst-pending",
          enqueued: "jst-pending",
          queued: "jst-queued",
          blocked: "jst-blocked",
        }[state] || "jst-pending"
      );
    }

    openJobLog(job) {
      window.open(`/cloud/log/${job.id}`, "_blank");
    }

    async cancelJob(ev, job) {
      ev.stopPropagation();
      const ok = await this._confirm({
        title: _t("Cancel job"),
        message: cancelPrompt(job),
        confirmLabel: _t("Cancel job"),
        isDanger: true,
      });
      if (ok) {
        try {
          await this.orm.call("cloud.job", "cancel_job", [job.id]);
        } catch (e) {
          this.env.toast?.error(_t("Cancel failed: ") + (e.data?.message || e.message));
        }
        this._loadJobs();
      }
    }

    async retryJob(ev, job) {
      ev.stopPropagation();
      try {
        const newJobId = await this.orm.call("cloud.job", "retry_job", [job.id]);
        if (newJobId) await announceJob(this.env.toast, newJobId);
      } catch (e) {
        this.env.toast?.error(_t("Retry failed: ") + (e.data?.message || e.message));
      }
      this._loadJobs();
    }

    async _loadJobs() {
      this.state.jobsLoading = true;
      try {
        const data = await this.orm.call("cloud.job", "get_instance_jobs", [
          this.props.instance_id,
          5,
          0,
        ]);
        this.state.activeJobs = data.activeJobs;
        this.state.recentJobs = data.recentJobs;
        this.state.jobsTotal = data.total;
      } catch (_e) {
        console.warn("Failed to load jobs:", _e);
      }
      this.state.jobsLoading = false;
    }

    get timelineJobs() {
      const MAX = 5;
      const active = this.state.activeJobs || [];
      const recent = this.state.recentJobs || [];
      const result = [];
      if (active.length <= MAX) {
        result.push(...[...active, ...recent].sort((a, b) => b.id - a.id));
      } else {
        const visibleCount = MAX - 2;
        result.push({
          _overflow: true,
          count: active.length - visibleCount - 1,
        });
        result.push(...active.slice(1, visibleCount + 1).reverse());
        result.push(active[0]);
      }
      return result;
    }

    get hasMoreJobs() {
      return (
        this.state.jobsTotal >
        (this.state.activeJobs || []).length + (this.state.recentJobs || []).length
      );
    }

    viewAllJobs() {
      this.env.toggleSlideOver("jobs", this.props.instance_id);
    }
  };
