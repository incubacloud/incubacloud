import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { patchTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc, RPCError } from "@web/core/network/rpc";
import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";
import { ProjectDetail } from "@incubacloud/components/project_detail/project_detail";

/**
 * A save the server refuses shows the server's reason.
 *
 * Creating or saving a project or an instance toasted only «Failed to
 * save …» whatever the server said, so a value its checks refuse gave no
 * clue. The ``save`` methods are the shipped ones, on a stand-in; the
 * server is ``rpc`` refusing as Odoo does.
 */

/**
 * Make ``rpc`` refuse every call with an ``RPCError``, as Odoo does.
 *
 * @param {object} data the error's ``data``: its ``name`` is the server's
 *     exception, such as ``odoo.exceptions.ValidationError``
 */
function serverRefuses(data) {
    patchWithCleanup(rpc, {
        _rpc: () => {
            const error = new RPCError("Odoo Server Error");
            error.data = data;
            return Promise.reject(error);
        },
    });
}

/**
 * The toasts a stand-in receives.
 *
 * @returns {{errors: Array, toast: object}}
 */
function toasts() {
    const errors = [];
    return {
        errors,
        toast: { error: (msg) => errors.push(String(msg)), success: () => {}, warning: () => {} },
    };
}

/**
 * A ProjectDetail stand-in, creating or saving.
 *
 * @param {boolean} isCreate whether the project is new
 * @returns {object} the stand-in, with ``errors``
 */
function projectDetail(isCreate) {
    const { errors, toast } = toasts();
    const comp = Object.create(ProjectDetail.prototype);
    Object.assign(comp, {
        errors,
        props: isCreate ? {} : { project_id: 4 },
        validator: { validate: () => ({ isValid: true }) },
        env: { toast, refreshAlertCount: () => {}, navigate: () => {} },
        state: {
            saving: false,
            form: { name: "Acme" },
            selectedTags: [],
            repos: [],
            pipDeps: "",
            aptDeps: "",
            members: [],
            project: { name: "Acme" },
        },
    });
    return comp;
}

/**
 * An InstanceDetail stand-in, creating or saving.
 *
 * @param {boolean} isCreate whether the instance is new
 * @returns {object} the stand-in, with ``errors``
 */
function instanceDetail(isCreate) {
    const { errors, toast } = toasts();
    const comp = Object.create(InstanceDetail.prototype);
    Object.assign(comp, {
        errors,
        props: isCreate ? { project_id: 4 } : { project_id: 4, instance_id: 9 },
        validator: { validate: () => ({ isValid: true }) },
        env: { toast },
        state: {
            saving: false,
            form: { name: "prod", smtp_relay_port: 587 },
            selectedTags: [],
            pipDeps: "",
            aptDeps: "",
            inst: { name: "prod" },
        },
    });
    return comp;
}

describe("a refused save says why", () => {
    beforeEach(() => patchTranslations());

    const REASON = "A project name cannot contain '{{', '{%', '{#' or invisible or control characters.";

    for (const isCreate of [true, false]) {
        const what = isCreate ? "create" : "save";

        test(`a project's ${what} shows the server's reason`, async () => {
            serverRefuses({ name: "odoo.exceptions.ValidationError", message: REASON });
            const comp = projectDetail(isCreate);
            await ProjectDetail.prototype.save.call(comp);
            expect(comp.errors).toEqual([REASON]);
            expect(comp.state.saving).toBe(false);
        });

        test(`an instance's ${what} shows the server's reason`, async () => {
            serverRefuses({
                name: "odoo.exceptions.UserError",
                message: "SMTP port 70000 is invalid. Use a port from 1 to 65535.",
            });
            const comp = instanceDetail(isCreate);
            await InstanceDetail.prototype.save.call(comp);
            expect(comp.errors).toEqual(["SMTP port 70000 is invalid. Use a port from 1 to 65535."]);
            expect(comp.state.saving).toBe(false);
        });
    }

    for (const [what, data] of [
        ["an error that is not Odoo's refusal", { name: "builtins.KeyError", message: "'vals'" }],
        ["an error without its exception's name", { message: "Odoo Server Error" }],
        ["an error without data", {}],
    ]) {
        test(`${what} shows the generic text`, async () => {
            serverRefuses(data);
            const project = projectDetail(false);
            await ProjectDetail.prototype.save.call(project);
            expect(project.errors).toEqual(["Failed to save project."]);
            const instance = instanceDetail(false);
            await InstanceDetail.prototype.save.call(instance);
            expect(instance.errors).toEqual(["Failed to save instance."]);
        });
    }

    test("a failure that is not an RPCError shows the generic text", async () => {
        patchWithCleanup(rpc, { _rpc: () => Promise.reject(new Error("network")) });
        const project = projectDetail(true);
        await ProjectDetail.prototype.save.call(project);
        expect(project.errors).toEqual(["Failed to create project."]);
        const instance = instanceDetail(true);
        await InstanceDetail.prototype.save.call(instance);
        expect(instance.errors).toEqual(["Failed to create instance."]);
    });
});
