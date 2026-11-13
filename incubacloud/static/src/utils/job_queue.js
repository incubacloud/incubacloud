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
import { parseUTC, userLocale } from "./dates";

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
 * Label of a job the channel gate holds back: first in its target's
 * queue, it waits for a free slot in queue_job's channel
 * (``channel_wait`` of ``_format``).
 *
 * @param {{channel: string, load: number, capacity: number|null}} wait
 *     the channel it waits on, how many of the panel's jobs take a place
 *     in it and how many it holds
 * @returns {string} "Queued · waiting for a free slot (root.user 1/1)"
 */
export function channelWaitLabel(wait) {
    const count = wait.capacity ? `${wait.load}/${wait.capacity}` : `${wait.load}`;
    return _t("Queued · waiting for a free slot (%s)", `${wait.channel} ${count}`);
}

/**
 * Label of a job first in a queue that waits for a step an ended job left
 * running on its server (``server_wait`` of ``_format``): past its point
 * of no return, the step is let end before the next job starts; on a
 * server that does not answer (``server_unanswered``), the panel waits
 * for it to answer and confirm the step stopped.
 *
 * @param {number} jobId the ended job's id
 * @param {boolean} [unanswered] the reconciliation could not reach that
 *     job's server
 * @returns {string} "Queued · waiting for job #41 to end on the server",
 *     or "Queued · waiting for the server to answer, to confirm job #41
 *     stopped"
 */
export function serverWaitLabel(jobId, unanswered = false) {
    if (unanswered) {
        return _t("Queued · waiting for the server to answer, to confirm job #%s stopped", jobId);
    }
    return _t("Queued · waiting for job #%s to end on the server", jobId);
}

/**
 * Label of a job another build on its server defers (``build_wait`` of
 * ``_format``): dispatched, it tries the host's build lock again every
 * 30 s, and names the job it waits for when the reader may read it.
 *
 * @param {{job_id: number|null, until: string|null}} wait the job it
 *     waits for, and when it tries again
 * @returns {string} "Pending · waiting for job #41 to finish building on
 *     this server", or for another build when no job is named
 */
export function buildWaitLabel(wait) {
    return wait.job_id
        ? _t("Pending · waiting for job #%s to finish building on this server", wait.job_id)
        : _t("Pending · waiting for another build on this server");
}

/**
 * Label of a job parked between two attempts: it runs, its step may go
 * on on its server (``on_server``), and the panel connects again at
 * ``parked_until``, written in the reader's time.
 *
 * @param {{on_server?: boolean, parked_until: string}} job a ``_format``
 *     or ``_format_history`` row
 * @returns {string} "Running · on the server, reconnecting at 12:03:05"
 */
export function parkedLabel(job) {
    const at = parseUTC(job.parked_until).toLocaleTimeString(userLocale(), {
        hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    return job.on_server
        ? _t("Running · on the server, reconnecting at %s", at)
        : _t("Running · reconnecting at %s", at);
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
 * Colour of a job's state in the recent activity of an instance or a
 * host: the Relay modifier of its timeline dot (``rl-tl-dot``) and
 * state pill (``rl-tl-state``).
 *
 * Green when done, red when failed, cyan (``running``, pulsing) while
 * it runs, amber while it waits: ``waiting`` in its target's queue,
 * ``queued`` for the rest (dispatched to a runner, or blocked on an
 * alert). A cancelled job has no colour, unless it still stops what it
 * left on its server (``stopping``): it runs until then.
 *
 * @param {string} state a ``cloud.job`` state, as ``_format`` gives it
 * @param {boolean} [stopping] ``stopping`` of the same row
 * @returns {string} "done" | "failed" | "running" | "waiting" | "queued" | ""
 */
export function jobTone(state, stopping = false) {
    if (state === "cancelled" && stopping) {
        return "running";
    }
    return {
        done: "done", failed: "failed", started: "running",
        queued: "waiting", cancelled: "",
    }[state] ?? "queued";
}

/**
 * Text of a job's state in the job lists and the history: its label;
 * for a job waiting in its queue how many entries go before it, the
 * channel slot it waits for when the gate holds it, or the ended job whose
 * step on the server its queue waits for; for a job another build on its
 * server defers, the job it waits for; for a job parked
 * between two attempts, when the panel connects again; for a job
 * cancelled that still stops what it left on its server (``stopping``),
 * "Cancelling…" until it has.
 *
 * @param {{state: string, queue_position?: number, channel_wait?: object|null,
 *     server_wait?: number|null, server_unanswered?: boolean,
 *     build_wait?: object|null, on_server?: boolean,
 *     parked_until?: string|null, stopping?: boolean}} job a ``_format``
 *     or ``_format_history`` row
 * @returns {string}
 */
export function jobStateText(job) {
    if (job.state === "cancelled" && job.stopping) {
        return _t("Cancelling…");
    }
    if (job.state === "queued") {
        if (job.channel_wait) {
            return channelWaitLabel(job.channel_wait);
        }
        return job.server_wait
            ? serverWaitLabel(job.server_wait, job.server_unanswered)
            : queuedLabel(job.queue_position);
    }
    if (job.state === "pending" && job.build_wait) {
        return buildWaitLabel(job.build_wait);
    }
    if (job.state === "started" && job.parked_until) {
        return parkedLabel(job);
    }
    return stateLabel(job.state);
}

/**
 * Why a job reads "Action required", in a sentence for its chip.
 *
 * Told by the code of the alert it needs a person for
 * (``action_alert_code`` of ``_format``): a failure holding its queue
 * (``job_failed``) names what failed, by its type
 * (``action_job_type_code``); a conflict says which; any other alert
 * reads the same generic sentence.
 *
 * @param {{action_alert_code?: string|null, action_job_type_code?: string|null}} job
 *     a ``_format`` row
 * @returns {string}
 */
export function holdReason(job) {
    const code = job?.action_alert_code;
    if (code === "job_failed") {
        return {
            deploy_instance: _t("Waiting: the previous deploy failed. Retry it or resolve its alert."),
            rebuild_instance: _t("Waiting: the previous rebuild failed. Retry it or resolve its alert."),
            restore_instance: _t("Waiting: the previous restore failed. Retry it or resolve its alert."),
            backup_restore: _t("Waiting: the previous restore failed. Retry it or resolve its alert."),
            delete_instance: _t("Waiting: the previous delete failed. Retry it or resolve its alert."),
        }[job.action_job_type_code]
            || _t("Waiting: an earlier job failed. Retry it or resolve its alert.");
    }
    if (code === "pip_conflict") {
        return _t("The Python dependencies conflict. Resolve its alert to go on.");
    }
    if (code === "addon_conflict") {
        return _t("A module is in more than one repository. Resolve its alert to go on.");
    }
    return _t("An alert needs you first. Resolve it to go on.");
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
 * Its colour is the job's: amber (``warning``) while it waits in the
 * queue, cyan (``info``) once it started.
 *
 * @returns {{type: string, message: string}|null} the toast to show: its
 *     type (``warning`` or ``info``) and text
 */
export function enqueueNotice(data) {
    const state = data?.state;
    if (state === "queued") {
        return {
            type: "warning",
            message: data.queued_behind
                ? _t("Queued behind %s", data.queued_behind)
                : _t("Queued"),
        };
    }
    if (RUNNING_STATES.includes(state) || state === "done" || state === "failed") {
        return { type: "info", message: _t("Started: %s", data.job_type || "") };
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
