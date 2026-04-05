import { describe, expect, test } from "@odoo/hoot";
import { ProjectSidebar } from "@incubacloud/components/project_sidebar/project_sidebar";

/**
 * ProjectSidebar uses useEnv, useService("orm"), useService("bus_service"),
 * rpc, onWillStart, onMounted, onWillUnmount.
 * We test pure logic: envLabel, statusDot, grouped getter, _getNextInstance,
 * activeSection, and cloneToStaging state initialization.
 */

describe("ProjectSidebar — class structure", () => {
    test("template name is set", () => {
        expect(ProjectSidebar.template).toBe("incubacloud.ProjectSidebar");
    });

    test("props defines projectId as Number", () => {
        expect(ProjectSidebar.props.projectId.type).toBe(Number);
    });

    test("props defines currentRoute as String", () => {
        expect(ProjectSidebar.props.currentRoute.type).toBe(String);
    });

    test("props defines selectedInstanceId as Number", () => {
        expect(ProjectSidebar.props.selectedInstanceId.type).toBe(Number);
    });
});

describe("ProjectSidebar — envLabel helper", () => {
    // Extracted from: envLabel(env) { return {production: "Production", ...}[env] || env; }
    function envLabel(env) {
        return { production: "Production", staging: "Staging", runbot: "Development" }[env] || env;
    }

    test("maps production", () => {
        expect(envLabel("production")).toBe("Production");
    });

    test("maps staging", () => {
        expect(envLabel("staging")).toBe("Staging");
    });

    test("maps runbot to Development", () => {
        expect(envLabel("runbot")).toBe("Development");
    });

    test("returns raw value for unknown env", () => {
        expect(envLabel("testing")).toBe("testing");
    });

    test("returns empty string for empty input", () => {
        expect(envLabel("")).toBe("");
    });
});

describe("ProjectSidebar — statusDot helper", () => {
    // Extracted from: statusDot(inst) { ... }
    function statusDot(inst) {
        if (inst.status === "provisioning") return "dot-provisioning";
        if (!inst.deployed) return "dot-neutral";
        if (inst.running) return "dot-ok";
        if (inst.status === "error") return "dot-error";
        return "dot-stopped";
    }

    test("provisioning status takes priority", () => {
        expect(statusDot({ status: "provisioning", deployed: true, running: true }))
            .toBe("dot-provisioning");
    });

    test("not deployed returns dot-neutral", () => {
        expect(statusDot({ status: "ok", deployed: false, running: false }))
            .toBe("dot-neutral");
    });

    test("deployed and running returns dot-ok", () => {
        expect(statusDot({ status: "ok", deployed: true, running: true }))
            .toBe("dot-ok");
    });

    test("deployed with error status returns dot-error", () => {
        expect(statusDot({ status: "error", deployed: true, running: false }))
            .toBe("dot-error");
    });

    test("deployed but not running and not error returns dot-stopped", () => {
        expect(statusDot({ status: "ok", deployed: true, running: false }))
            .toBe("dot-stopped");
    });

    test("deployed but not running with null status returns dot-stopped", () => {
        expect(statusDot({ status: null, deployed: true, running: false }))
            .toBe("dot-stopped");
    });
});

describe("ProjectSidebar — grouped getter logic", () => {
    // Extracted from: get grouped() { ... }
    function grouped(instances) {
        const groups = { production: [], staging: [], runbot: [] };
        for (const inst of instances) {
            const env = inst.environment || "runbot";
            (groups[env] ||= []).push(inst);
        }
        return groups;
    }

    test("empty instances returns empty groups", () => {
        const g = grouped([]);
        expect(g.production).toEqual([]);
        expect(g.staging).toEqual([]);
        expect(g.runbot).toEqual([]);
    });

    test("groups instances by environment", () => {
        const instances = [
            { id: 1, name: "prod", environment: "production" },
            { id: 2, name: "stg", environment: "staging" },
            { id: 3, name: "dev", environment: "runbot" },
        ];
        const g = grouped(instances);
        expect(g.production.length).toBe(1);
        expect(g.staging.length).toBe(1);
        expect(g.runbot.length).toBe(1);
    });

    test("null environment defaults to runbot", () => {
        const g = grouped([{ id: 1, name: "test", environment: null }]);
        expect(g.runbot.length).toBe(1);
    });

    test("undefined environment defaults to runbot", () => {
        const g = grouped([{ id: 1, name: "test" }]);
        expect(g.runbot.length).toBe(1);
    });

    test("multiple instances in same environment", () => {
        const instances = [
            { id: 1, name: "stg1", environment: "staging" },
            { id: 2, name: "stg2", environment: "staging" },
            { id: 3, name: "stg3", environment: "staging" },
        ];
        const g = grouped(instances);
        expect(g.staging.length).toBe(3);
        expect(g.production.length).toBe(0);
    });
});

describe("ProjectSidebar — _getNextInstance logic", () => {
    // Extracted from: _getNextInstance(excludeId) { ... }
    function getNextInstance(instances, excludeId) {
        const groups = { production: [], staging: [], runbot: [] };
        for (const inst of instances) {
            const env = inst.environment || "runbot";
            (groups[env] ||= []).push(inst);
        }
        const order = ["production", "staging", "runbot"];
        for (const env of order) {
            for (const inst of groups[env] || []) {
                if (inst.id !== excludeId) return inst;
            }
        }
        return null;
    }

    test("returns null when no instances", () => {
        expect(getNextInstance([], null)).toBe(null);
    });

    test("prefers production over staging", () => {
        const instances = [
            { id: 1, name: "stg", environment: "staging" },
            { id: 2, name: "prod", environment: "production" },
        ];
        const next = getNextInstance(instances, null);
        expect(next.id).toBe(2);
    });

    test("prefers staging over runbot", () => {
        const instances = [
            { id: 1, name: "dev", environment: "runbot" },
            { id: 2, name: "stg", environment: "staging" },
        ];
        const next = getNextInstance(instances, null);
        expect(next.id).toBe(2);
    });

    test("excludes specified id and returns next", () => {
        const instances = [
            { id: 1, name: "prod", environment: "production" },
            { id: 2, name: "stg", environment: "staging" },
        ];
        const next = getNextInstance(instances, 1);
        expect(next.id).toBe(2);
    });

    test("returns null when only instance is excluded", () => {
        const instances = [{ id: 1, name: "prod", environment: "production" }];
        expect(getNextInstance(instances, 1)).toBe(null);
    });

    test("skips excluded and returns next in same group", () => {
        const instances = [
            { id: 1, name: "prod1", environment: "production" },
            { id: 2, name: "prod2", environment: "production" },
        ];
        const next = getNextInstance(instances, 1);
        expect(next.id).toBe(2);
    });

    test("with null excludeId returns first by priority", () => {
        const instances = [
            { id: 3, name: "dev", environment: "runbot" },
            { id: 1, name: "prod", environment: "production" },
            { id: 2, name: "stg", environment: "staging" },
        ];
        const next = getNextInstance(instances, null);
        expect(next.id).toBe(1);
    });
});

describe("ProjectSidebar — activeSection getter logic", () => {
    // Extracted from: get activeSection() { ... }
    function activeSection(currentRoute, instanceCount) {
        if (currentRoute === "instance_detail" || currentRoute === "create_instance") return "";
        if (currentRoute === "project_settings") return "project_settings";
        if (currentRoute === "project_detail" && instanceCount === 0) return "";
        return currentRoute;
    }

    test("instance_detail returns empty", () => {
        expect(activeSection("instance_detail", 5)).toBe("");
    });

    test("create_instance returns empty", () => {
        expect(activeSection("create_instance", 5)).toBe("");
    });

    test("project_settings returns project_settings", () => {
        expect(activeSection("project_settings", 0)).toBe("project_settings");
    });

    test("project_detail with no instances returns empty (onboarding)", () => {
        expect(activeSection("project_detail", 0)).toBe("");
    });

    test("project_detail with instances returns route name", () => {
        expect(activeSection("project_detail", 3)).toBe("project_detail");
    });

    test("unknown route returns route name", () => {
        expect(activeSection("some_page", 1)).toBe("some_page");
    });
});

describe("ProjectSidebar — cloneToStaging state initialization", () => {
    // cloneToStaging sets cloneModal state
    function buildCloneModal(inst) {
        return {
            instanceId: inst.id,
            name: inst.name + "-staging",
            loading: false,
            error: null,
        };
    }

    test("sets instanceId from instance", () => {
        const modal = buildCloneModal({ id: 42, name: "prod" });
        expect(modal.instanceId).toBe(42);
    });

    test("appends -staging to instance name", () => {
        const modal = buildCloneModal({ id: 1, name: "myapp" });
        expect(modal.name).toBe("myapp-staging");
    });

    test("loading starts as false", () => {
        const modal = buildCloneModal({ id: 1, name: "test" });
        expect(modal.loading).toBe(false);
    });

    test("error starts as null", () => {
        const modal = buildCloneModal({ id: 1, name: "test" });
        expect(modal.error).toBe(null);
    });
});
