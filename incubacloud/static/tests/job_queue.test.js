import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";
import { HostDetail } from "@incubacloud/components/host_detail/host_detail";
import { JobHistory } from "@incubacloud/components/job_history/job_history";
import { createToastService } from "@incubacloud/toast/toast";
import {
    announceJob,
    enqueueNotice,
    queuedLabel,
    stateLabel,
} from "@incubacloud/utils/job_queue";

/**
 * The job queue as the panel shows it (core 1.0.198).
 *
 * An instance runs one job at a time; a job asked for meanwhile waits
 * ``queued`` in its queue. The lists say where it is in line, a queued
 * job can be cancelled from them, a queue held by a failure says so
 * and leads to the alert, and a button that enqueues says whether its
 * job started or waits — and opens no tab: the log opens from the list.
 *
 * No mounting (see ic_confirm_dialog.test.js): the methods under test
 * are the shipped ones, bound to a stand-in that records what they
 * reach for; the toasts are the panel's own service, and the server is
 * ``rpc`` answering what each test says.
 */

const STARTED = {
    ok: true, job_id: 5, state: "pending", job_type: "Deploy Instance", queued_behind: false,
};
const QUEUED = {
    ok: true, job_id: 6, state: "queued", job_type: "Restore Backup",
    queued_behind: "Rebuild Instance",
};
const QUEUED_ALONE = {
    ok: true, job_id: 7, state: "queued", job_type: "Rebuild Instance", queued_behind: false,
};

/**
 * The panel's toast service, with what it shows read back.
 *
 * @returns {object} the service's API (``env.toast``), plus ``shown()``:
 *     the toasts on screen as ``[type, message]`` pairs
 */
function toastService() {
    const { toastApi, toasts } = createToastService();
    return {
        ...toastApi,
        shown: () => toasts.map((t) => [t.type, String(t.message)]),
    };
}

/**
 * Have the server answer *answers* through ``rpc`` for this test.
 *
 * @param {object} answers by route: the answer, a function of the
 *     request's params returning it, or an ``Error`` to reject with
 */
function answering(answers) {
    patchWithCleanup(rpc, {
        _rpc(url, params) {
            const answer = answers[url];
            if (answer instanceof Error) return Promise.reject(answer);
            return Promise.resolve(
                typeof answer === "function" ? answer(params) : answer,
            );
        },
    });
}

/**
 * Run *fn* with ``window.open`` recorded instead of opening anything.
 *
 * @param {Function} fn async function to run
 * @returns {Promise<Array>} the arguments of every ``window.open`` call
 */
async function recordingTabs(fn) {
    const opened = [];
    const original = window.open;
    window.open = (...args) => {
        opened.push(args);
        return null;
    };
    try {
        await fn();
    } finally {
        window.open = original;
    }
    return opened;
}

/**
 * An InstanceDetail stand-in whose enqueue answers *response*.
 *
 * ``_enqueueJob`` is the shipped one; only the request it makes is
 * replaced, so what the buttons pass to it and what it does with the
 * answer are both the real code's.
 *
 * @param {object} response what the endpoint answers
 * @returns {object} the stand-in, with ``toast``, ``options`` and ``tabs``
 */
function instanceDetail(response) {
    const comp = Object.create(InstanceDetail.prototype);
    comp.toast = toastService();
    comp.options = [];
    comp.tabs = [];
    comp.props = { instance_id: 3 };
    comp.state = { inst: { name: "prod", host_id: 1 } };
    comp.env = { toast: comp.toast };
    comp._silentRefresh = () => {};
    comp.setTab = (tab) => comp.tabs.push(tab);
    comp._enqueueJob = (fn, opts) => {
        comp.options.push(opts || {});
        return InstanceDetail.prototype._enqueueJob.call(
            comp, () => Promise.resolve(response), opts,
        );
    };
    return comp;
}

describe("job queue — labels", () => {
    beforeEach(() => allowTranslations());

    test("a queued job says how many go before it", () => {
        expect(String(queuedLabel(0))).toBe("Queued · 0 ahead");
        expect(String(queuedLabel(2))).toBe("Queued · 2 ahead");
        expect(String(queuedLabel(undefined))).toBe("Queued · 0 ahead");
    });

    test("the instance's list shows the position of a queued job", () => {
        const comp = Object.create(InstanceDetail.prototype);
        expect(String(comp.jobStateText({ state: "queued", queue_position: 1 }))).toBe(
            "Queued · 1 ahead",
        );
    });

    test("the lists and the history read every state as its label", () => {
        for (const proto of [InstanceDetail.prototype, HostDetail.prototype]) {
            const comp = Object.create(proto);
            expect(String(comp.jobStateText({ state: "started" }))).toBe("Running");
            expect(String(comp.jobStateText({ state: "blocked" }))).toBe("Blocked");
        }
        const history = Object.create(JobHistory.prototype);
        expect(String(history.rowStateText({ state: "started" }))).toBe("Running");
        expect(String(stateLabel("enqueued"))).toBe("Starting");
        expect(stateLabel("custom")).toBe("custom");
    });

    test("the host's list shows it too", () => {
        const comp = Object.create(HostDetail.prototype);
        expect(String(comp.jobStateText({ state: "queued", queue_position: 3 }))).toBe(
            "Queued · 3 ahead",
        );
    });

    test("the history shows it and keeps queue_job's enqueued apart", () => {
        const comp = Object.create(JobHistory.prototype);
        expect(String(comp.rowStateText({ state: "queued", queue_position: 0 }))).toBe(
            "Queued · 0 ahead",
        );
        expect(String(comp.rowStateText({ state: "enqueued" }))).toBe("Starting");
        expect(String(comp.stateLabel("queued"))).toBe("Queued");
        expect(comp.ganttDotClass("queued")).toBe("waiting");
    });

    test("a queued job counts as active and has its own state class", () => {
        for (const proto of [InstanceDetail.prototype, HostDetail.prototype]) {
            expect(proto.isActiveJob("queued")).toBe(true);
            expect(proto.jobStateClass("queued")).toBe("jst-queued");
        }
    });
});

describe("job queue — a queue held by a failure", () => {
    test("the waiting job leads to the alert, as a blocked one does", () => {
        const comp = Object.create(InstanceDetail.prototype);
        expect(comp.waitsOnAlert({ state: "queued", queue_held: true })).toBe(true);
        expect(comp.waitsOnAlert({ state: "blocked" })).toBe(true);
        expect(comp.waitsOnAlert({ state: "queued", queue_held: false })).toBe(false);
        expect(comp.waitsOnAlert({ state: "started" })).toBe(false);
    });

    test("Resolve opens the alerts", () => {
        const comp = Object.create(InstanceDetail.prototype);
        const panels = [];
        comp.env = { toggleSlideOver: (panel) => panels.push(panel) };
        comp.goResolveAlert({ stopPropagation() {} });
        expect(panels).toEqual(["alerts"]);
    });
});

describe("job queue — cancelling a queued job", () => {
    beforeEach(() => allowTranslations());

    /**
     * A stand-in for the instance's or the host's list.
     *
     * @param {object} proto the component's prototype
     * @param {object} [opts]
     * @param {object} [opts.refusal] what ``cancel_job`` rejects with
     * @returns {object} the stand-in, with ``asked`` and ``called``
     */
    function list(proto, { refusal } = {}) {
        const comp = Object.create(proto);
        comp.asked = [];
        comp.called = [];
        comp.toast = toastService();
        comp.env = { toast: comp.toast };
        comp._confirm = (opts) => {
            comp.asked.push(opts);
            return Promise.resolve(true);
        };
        comp.orm = {
            call: (model, method, args) => {
                comp.called.push([model, method, args]);
                return refusal ? Promise.reject(refusal) : Promise.resolve(true);
            },
        };
        comp._loadJobs = () => {};
        return comp;
    }

    const QUEUED_JOB = { id: 9, name: "Restart Instance", state: "queued" };

    for (const [name, proto] of [
        ["instance", InstanceDetail.prototype],
        ["host", HostDetail.prototype],
    ]) {
        test(`the ${name}'s list says it has not started and takes it out`, async () => {
            const comp = list(proto);
            await comp.cancelJob({ stopPropagation() {} }, QUEUED_JOB);
            expect(String(comp.asked[0].message)).toBe(
                '"Restart Instance" has not started yet. Cancel it and take it out of the queue?',
            );
            expect(comp.called).toEqual([["cloud.job", "cancel_job", [9]]]);
        });

        test(`the ${name}'s list warns that a step takes its chain with it`, async () => {
            const comp = list(proto);
            await comp.cancelJob(
                { stopPropagation() {} },
                { id: 10, name: "Move Cutover", state: "queued", in_chain: true },
            );
            await comp.cancelJob(
                { stopPropagation() {} },
                { id: 11, name: "Move Cutover", state: "wait_dependencies", in_chain: true },
            );
            expect(comp.asked.map((opts) => String(opts.message))).toEqual([
                '"Move Cutover" has not started yet. Cancel it and take it out of the queue?'
                + " It is a step of a chain: the rest of its chain is cancelled with it.",
                'Cancel "Move Cutover"? This cannot be undone.'
                + " It is a step of a chain: the rest of its chain is cancelled with it.",
            ]);
        });

        test(`the ${name}'s list shows the server's refusal`, async () => {
            const comp = list(proto, {
                refusal: { data: { message: "Only a developer may cancel this job." } },
            });
            await comp.cancelJob({ stopPropagation() {} }, QUEUED_JOB);
            expect(comp.toast.shown()).toEqual([
                ["error", "Cancel failed: Only a developer may cancel this job."],
            ]);
        });
    }

    test("a running job keeps its own warning", async () => {
        const comp = list(InstanceDetail.prototype);
        await comp.cancelJob({ stopPropagation() {} }, { id: 4, name: "Rebuild", state: "started" });
        expect(String(comp.asked[0].message)).toBe('Cancel "Rebuild"? This cannot be undone.');
    });
});

describe("job queue — the notice after a button", () => {
    beforeEach(() => allowTranslations());

    /**
     * The notice for *data*, its message as a plain string.
     *
     * @param {object|null} data an endpoint's answer
     * @returns {Array|null} ``[type, message]``, or null for no notice
     */
    function notice(data) {
        const shown = enqueueNotice(data);
        return shown && [shown.type, String(shown.message)];
    }

    test("a job that started says so, with its type", () => {
        expect(notice(STARTED)).toEqual(["success", "Started: Deploy Instance"]);
        expect(notice({ ...STARTED, state: "started" })).toEqual([
            "success", "Started: Deploy Instance",
        ]);
    });

    test("a queued job says what it waits behind", () => {
        expect(notice(QUEUED)).toEqual(["info", "Queued behind Rebuild Instance"]);
    });

    test("a queued job with nothing visible ahead says it is queued", () => {
        expect(notice(QUEUED_ALONE)).toEqual(["info", "Queued"]);
    });

    test("a blocked, cancelled or unreadable job says nothing", () => {
        expect(notice({ state: "blocked", blocked: true })).toBe(null);
        expect(notice({ state: "cancelled" })).toBe(null);
        expect(notice({ ok: false })).toBe(null);
        expect(notice(null)).toBe(null);
    });

    test("announceJob shows the endpoint's answer as it is", async () => {
        const toast = toastService();
        await announceJob(toast, QUEUED);
        expect(toast.shown()).toEqual([["info", "Queued behind Rebuild Instance"]]);
    });

    test("_enqueueJob shows the notice of every answer it gets", async () => {
        for (const [response, expected] of [
            [STARTED, ["success", "Started: Deploy Instance"]],
            [QUEUED, ["info", "Queued behind Rebuild Instance"]],
            [QUEUED_ALONE, ["info", "Queued"]],
        ]) {
            const comp = instanceDetail(response);
            await comp._enqueueJob(() => Promise.resolve(response));
            expect(comp.toast.shown()).toEqual([expected]);
        }
    });
});

describe("job queue — no button opens the log", () => {
    beforeEach(() => allowTranslations());

    test("deploy opens no tab and says it started", async () => {
        const comp = instanceDetail(STARTED);
        const tabs = await recordingTabs(() => comp.deployInstance());
        expect(tabs).toEqual([]);
        expect(comp.options[0].openLog).toBe(undefined);
        expect(comp.toast.shown()).toEqual([["success", "Started: Deploy Instance"]]);
    });

    test("rebuild opens no tab and says it is queued", async () => {
        const comp = instanceDetail(QUEUED_ALONE);
        const tabs = await recordingTabs(() => comp.rebuildInstance());
        expect(tabs).toEqual([]);
        expect(comp.options[0].openLog).toBe(undefined);
        expect(comp.toast.shown()).toEqual([["info", "Queued"]]);
    });

    test("restore opens no tab and says what it waits behind", async () => {
        const comp = instanceDetail(QUEUED);
        comp.state.restoreBackupModal = { time: "20261001T000000Z", confirmName: "prod", loading: false };
        const tabs = await recordingTabs(() => comp.doRestoreBackup());
        expect(tabs).toEqual([]);
        expect(comp.options[0].openLog).toBe(undefined);
        expect(comp.toast.shown()).toEqual([["info", "Queued behind Rebuild Instance"]]);
        expect(comp.tabs).toEqual(["overview"]);
        expect(comp.state.restoreBackupModal).toBe(null);
    });
});

describe("job queue — every button that enqueues says what became of its job", () => {
    /** What ``/cloud/get_job_state`` answers, per job id. */
    const STATES = {
        21: { ok: true, job_id: 21, state: "pending", job_type: "Host Probe", queued_behind: false },
        22: { ok: true, job_id: 22, state: "queued", job_type: "Setup Host", queued_behind: "Harden Host" },
        23: { ok: true, job_id: 23, state: "queued", job_type: "Move Cutover", queued_behind: "Rebuild Instance" },
        24: { ok: true, job_id: 24, state: "queued", job_type: "Move Rollback Cleanup", queued_behind: false },
        25: { ok: true, job_id: 25, state: "queued", job_type: "Grant Restore Upload Key", queued_behind: "Rebuild Instance" },
        26: { ok: true, job_id: 26, state: "pending", job_type: "Grant Restore Upload Key", queued_behind: false },
        27: { ok: true, job_id: 27, state: "pending", job_type: "Restore Instance", queued_behind: false },
    };

    /** ``/cloud/get_job_state`` as the server answers it here. */
    const STATE_ROUTE = {
        "/cloud/get_job_state": ({ job_id }) => STATES[job_id] || { ok: false },
    };

    beforeEach(() => {
        allowTranslations();
        answering(STATE_ROUTE);
    });

    /**
     * A HostDetail stand-in whose ``cloud.job.enqueue`` answers *jobId*.
     *
     * @param {number} jobId the id the ORM call returns
     * @returns {object} the stand-in, with ``toast``
     */
    function hostDetail(jobId) {
        const comp = Object.create(HostDetail.prototype);
        comp.toast = toastService();
        comp.props = { host_id: 7 };
        comp.state = { actionBusy: "" };
        comp.env = { toast: comp.toast };
        comp.orm = { call: () => Promise.resolve(jobId) };
        comp._silentRefresh = () => {};
        return comp;
    }

    test("a bare job id is read back before it is announced", async () => {
        const toast = toastService();
        await announceJob(toast, 22);
        expect(toast.shown()).toEqual([["info", "Queued behind Harden Host"]]);
    });

    test("no job, or one that cannot be read, says nothing", async () => {
        const toast = toastService();
        await announceJob(toast, undefined);
        await announceJob(toast, false);
        await announceJob(toast, 99);
        expect(toast.shown()).toEqual([]);
    });

    test("the host's check says it started", async () => {
        const comp = hostDetail(21);
        await comp.runCheck();
        expect(comp.toast.shown()).toEqual([["success", "Started: Host Probe"]]);
    });

    test("the host's Full Setup says what it waits behind", async () => {
        const comp = hostDetail(22);
        await comp.runFullSetup();
        expect(comp.toast.shown()).toEqual([["info", "Queued behind Harden Host"]]);
    });

    test("a host action no longer calls a started job queued", async () => {
        const comp = hostDetail(21);
        await comp.hostAction({ code: "host_probe", name: "Host Probe" });
        expect(comp.toast.shown()).toEqual([["success", "Started: Host Probe"]]);
    });

    /**
     * An InstanceDetail stand-in whose routes answer from *answers*.
     *
     * @param {object} answers the answer of each route, by path
     * @returns {object} the stand-in, with ``toast``
     */
    function moving(answers) {
        answering({ ...STATE_ROUTE, ...answers });
        const comp = Object.create(InstanceDetail.prototype);
        comp.toast = toastService();
        comp.props = { instance_id: 3 };
        comp.state = { inst: { move_origin_host: "old-host" }, movePicker: { targetHostId: 9 } };
        comp.env = { toast: comp.toast };
        comp._confirm = () => Promise.resolve(true);
        comp._silentRefresh = () => {};
        comp.closeMovePicker = () => {};
        return comp;
    }

    test("a move that waits its turn says so, not that it started", async () => {
        const comp = moving({ "/cloud/move_instance": { ok: true, job_id: 23 } });
        await comp.confirmMove();
        expect(comp.toast.shown()).toEqual([["info", "Queued behind Rebuild Instance"]]);
    });

    test("a rollback says what became of its recovery jobs", async () => {
        const comp = moving({ "/cloud/rollback_move": { ok: true, job_id: 24 } });
        await comp.rollbackMove();
        expect(comp.toast.shown()).toEqual([["info", "Queued"]]);
    });

    /**
     * An InstanceDetail stand-in with the SSH restore dialog open, whose
     * key is installed by job *jobId*.
     *
     * @param {number} jobId the job ``grant_restore_upload`` answers
     * @returns {object} the stand-in, with ``toast``
     */
    function uploading(jobId) {
        const comp = Object.create(InstanceDetail.prototype);
        comp.toast = toastService();
        comp.env = { toast: comp.toast };
        comp.props = { instance_id: 3 };
        comp.orm = {
            call: () => Promise.resolve({ grant_id: 4, job_id: jobId, private_key: "KEY" }),
        };
        comp.state = {
            restoreDialog: { granting: false, grant: null, privateKey: "", keyQueued: false },
        };
        return comp;
    }

    test("an upload key whose job waits says so, and so does the dialog", async () => {
        const comp = uploading(25);
        await comp.requestUploadKey();
        expect(comp.toast.shown()).toEqual([["info", "Queued behind Rebuild Instance"]]);
        expect(comp.state.restoreDialog.privateKey).toBe("KEY");
        expect(comp.state.restoreDialog.keyQueued).toBe(true);
    });

    test("an upload key whose job started leaves the dialog as it was", async () => {
        const comp = uploading(26);
        await comp.requestUploadKey();
        expect(comp.toast.shown()).toEqual([["success", "Started: Grant Restore Upload Key"]]);
        expect(comp.state.restoreDialog.keyQueued).toBe(false);
    });

    /**
     * Let every promise chain that can advance do so.
     *
     * @returns {Promise<void>}
     */
    async function settle() {
        for (let i = 0; i < 30; i++) await Promise.resolve();
    }

    /**
     * An InstanceDetail stand-in whose SSH dialog holds a check of the
     * upload from before, and whose ``verify_restore_upload`` job *jobId*
     * ends only when the test says so.
     *
     * @param {object} host what the host reports, and in which state the
     *     check job is (both change as the test goes on)
     * @returns {object} the stand-in, with ``reads`` (how many times the
     *     grant was read) and ``ticks`` (the polls waiting to run)
     */
    function checking(host) {
        answering({
            "/cloud/get_job_state": () => ({
                ok: true, state: host.jobState, job_type: "Verify Restore Upload",
                queued_behind: false,
            }),
        });
        const comp = Object.create(InstanceDetail.prototype);
        comp.toast = toastService();
        comp.env = { toast: comp.toast };
        comp.props = { instance_id: 3 };
        comp.reads = 0;
        comp.ticks = [];
        comp._safePoll = { alive: true, schedule: (fn) => comp.ticks.push(fn) };
        comp.orm = {
            call: (model, method) => {
                if (method === "verify_restore_upload") return Promise.resolve(30);
                comp.reads += 1;
                return Promise.resolve([host.grant]);
            },
        };
        comp.state = {
            restoreDialog: {
                grant: { grant_id: 4 }, checking: false, error: null,
                received: { filename: "restore.zip", bytes: 10, sha256: "old" },
            },
        };
        return comp;
    }

    test("a check of the upload shows nothing until its job has ended", async () => {
        const host = { jobState: "started", grant: { received_filename: false } };
        const comp = checking(host);
        const dialog = comp.state.restoreDialog;
        const done = comp.checkUpload();
        await settle();
        // The job still runs: the file from the last check is not shown
        // nor restorable, and the grant was not read yet.
        expect(dialog.received).toBe(null);
        expect(dialog.checking).toBe(true);
        expect(comp.reads).toBe(0);
        expect(comp.ticks.length).toBe(1);
        host.jobState = "done";
        comp.ticks.shift()();
        await done;
        expect(comp.reads).toBe(1);
        expect(dialog.received).toBe(null);
        expect(dialog.checking).toBe(false);
    });

    test("what the finished check found is what the dialog shows", async () => {
        const host = {
            jobState: "done",
            grant: { received_filename: "restore.zip", received_bytes: 20, received_sha256: "new" },
        };
        const comp = checking(host);
        await comp.checkUpload();
        expect(comp.state.restoreDialog.received).toEqual(
            { filename: "restore.zip", bytes: 20, sha256: "new" },
        );
        expect(comp.state.restoreDialog.checking).toBe(false);
    });

    test("closing the dialog while the check waits abandons the wait", async () => {
        const host = { jobState: "queued", grant: { received_filename: "restore.zip" } };
        const comp = checking(host);
        const done = comp.checkUpload();
        await settle();
        comp.state.restoreDialog = null;
        host.jobState = "done";
        comp.ticks.shift()();
        await done;
        expect(comp.reads).toBe(0);
    });

    test("a restore through the key names the digest the dialog showed", async () => {
        const comp = Object.create(InstanceDetail.prototype);
        comp.toast = toastService();
        comp.env = { toast: comp.toast };
        comp.props = { instance_id: 3 };
        const calls = [];
        comp.orm = {
            call: (...args) => {
                calls.push(args);
                return Promise.resolve(27);
            },
        };
        comp.state = {
            restoreDialog: {
                mode: "ssh", uploading: false, error: null, backupBeforeRestore: false,
                grant: { grant_id: 4 },
                received: { filename: "restore.zip", bytes: 20, sha256: "seen" },
            },
        };
        await comp.doRestore();
        expect(calls).toEqual([[
            "cloud.instance", "restore_db",
            [[3], {
                mode: "ssh_upload", grant_id: 4, backup_before_restore: false,
                expected_sha256: "seen",
            }],
        ]]);
        expect(comp.state.restoreDialog).toBe(null);
    });

    test("a rollback of a move that had not started says it is undone", async () => {
        const comp = moving({ "/cloud/rollback_move": { ok: true, job_id: null } });
        await comp.rollbackMove();
        expect(comp.toast.shown()).toEqual([["success", "Move rolled back."]]);
    });
});

describe("job queue — a state that cannot be read is not announced", () => {
    beforeEach(() => {
        allowTranslations();
        answering({ "/cloud/get_job_state": new Error("the connection dropped") });
    });

    test("announceJob resolves without a toast", async () => {
        const toast = toastService();
        expect(await announceJob(toast, 5)).toBe(null);
        expect(toast.shown()).toEqual([]);
    });

    test("_enqueueJob shows no error for a job that was queued", async () => {
        const answer = { ok: true, job_id: 5 };
        const comp = instanceDetail(answer);
        expect(await comp._enqueueJob(() => Promise.resolve(answer))).toEqual(answer);
        expect(comp.toast.shown()).toEqual([]);
    });
});
