/**
 * job_queue — how the panel words a job in its target's queue.
 *
 * An instance runs one job at a time, and a host one host-level job at
 * a time: a job asked for while another runs waits ``queued`` and
 * starts when the running one ends. The job lists, the history and the
 * notice after a button that enqueues say it the same way, so the
 * words live here and are tested on their own.
 */
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

/** Dispatched to queue_job and not ended (``cloud.job._running_states``). */
export const RUNNING_STATES = ["pending", "enqueued", "wait_dependencies", "started"];

/**
 * Waiting in its target's queue or running on it (``cloud.job._active_states``):
 * what the lists count as active and offer to cancel.
 */
export const ACTIVE_STATES = ["queued", ...RUNNING_STATES];

/**
 * Label of a job waiting in its queue.
 *
 * @param {number|null|undefined} position how many entries are
 *     dispatched before it (``queue_position``); missing reads as none
 * @returns {string} "Queued · N ahead"
 */
export function queuedLabel(position) {
    return _t("Queued · %s ahead", position || 0);
}

/**
 * Label of a job's state, in the reader's language.
 *
 * ``queued`` waits in its target's queue, with no queue.job yet.
 * ``enqueued`` is queue_job's: a runner took the job and is about to
 * run it, hence "Starting" — "Queued" is the target's queue now.
 * ``blocked`` waits on an alert (``_format``). Any other state reads as
 * it is.
 *
 * @param {string} state a ``cloud.job`` state, as ``_format`` gives it
 * @returns {string}
 */
export function stateLabel(state) {
    return {
        queued: _t("Queued"), pending: _t("Pending"), enqueued: _t("Starting"),
        wait_dependencies: _t("Waiting"), started: _t("Running"),
        done: _t("Done"), failed: _t("Failed"), cancelled: _t("Cancelled"),
        blocked: _t("Blocked"),
    }[state] || state;
}

/**
 * Text of a job's state in the job lists and the history: its label,
 * and for a job waiting in its queue how many entries go before it.
 *
 * @param {{state: string, queue_position?: number}} job a ``_format`` or
 *     ``_format_history`` row
 * @returns {string}
 */
export function jobStateText(job) {
    return job.state === "queued" ? queuedLabel(job.queue_position) : stateLabel(job.state);
}

/**
 * The question asked before cancelling a job from a list.
 *
 * A job waiting in its queue has not started: cancelling it takes it
 * out of the queue at once, and nothing reaches the host. A step of a
 * chain (``in_chain``) takes the rest of its chain with it, waiting or
 * not.
 *
 * @param {{name: string, state: string, in_chain?: boolean}} job a
 *     ``_format`` row
 * @returns {string} the confirmation's message
 */
export function cancelPrompt(job) {
    let message = (
        job.state === "queued"
            ? _t('"%s" has not started yet. Cancel it and take it out of the queue?')
            : _t('Cancel "%s"? This cannot be undone.')
    ).replace("%s", job.name);
    if (job.in_chain) {
        message += " " + _t("It is a step of a chain: the rest of its chain is cancelled with it.");
    }
    return message;
}

/**
 * What to tell the person who just asked for a job.
 *
 * Started when its queue.job exists (whatever it reached since);
 * queued, behind the job running on its target when one is named. A
 * job blocked on an alert has its own dialog, and a job that ended
 * cancelled or could not be read says nothing.
 *
 * @param {{state?: string, job_type?: string, queued_behind?: string|false}|null} data
 *     the answer of an endpoint that enqueues (``_job_response``) or of
 *     ``/cloud/get_job_state``
 * @returns {{type: string, message: string}|null} the toast to show: its
 *     type (``success`` or ``info``) and text
 */
export function enqueueNotice(data) {
    const state = data?.state;
    if (state === "queued") {
        return {
            type: "info",
            message: data.queued_behind
                ? _t("Queued behind %s", data.queued_behind)
                : _t("Queued"),
        };
    }
    if (RUNNING_STATES.includes(state) || state === "done" || state === "failed") {
        return { type: "success", message: _t("Started: %s", data.job_type || "") };
    }
    return null;
}

/**
 * Tell the person what became of the job they just asked for.
 *
 * The answer of an endpoint that enqueues already carries the state;
 * a bare job id (the ORM's ``enqueue``, a retry, an endpoint that
 * answers with the id alone) is looked up through
 * ``/cloud/get_job_state`` once its request committed. Anything else
 * (no id: nothing was queued) says nothing. Never raises: the job is
 * queued whatever this says, and a state that cannot be read is not
 * announced.
 *
 * @param {object} toast the toast service (``env.toast``)
 * @param {object|number|null|undefined} job the endpoint's answer, or
 *     the job's id
 * @returns {Promise<object|null>} the answer read, for a caller that
 *     shows more than the notice, whether or not it made one: the
 *     endpoint's own, or ``/cloud/get_job_state``'s for an id (``{ok:
 *     false}`` for a job the caller cannot read). Null when there was
 *     nothing to read (no id) or the state could not be read.
 */
export async function announceJob(toast, job) {
    let data = job;
    if (typeof job === "number") {
        try {
            data = await rpc("/cloud/get_job_state", { job_id: job });
        } catch {
            return null;
        }
    }
    const notice = enqueueNotice(data);
    if (notice) toast?.[notice.type](notice.message);
    return data && typeof data === "object" ? data : null;
}
