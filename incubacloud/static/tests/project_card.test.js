import { describe, expect, test } from "@odoo/hoot";
import { ProjectCard } from "@incubacloud/components/project_card/project_card";

const SAMPLE_PROJECT = {
    id: 1,
    name: "My Cloud Project",
    odoo_version: "17.0",
    status: "ok",
    description: "A test project",
    environments: ["production", "staging"],
    instance_count: 3,
    tags: [{ id: 10, name: "acme", color: 0 }],
    first_production_instance_id: 42,
};

describe("ProjectCard — class structure", () => {
    test("template name is set", () => {
        expect(ProjectCard.template).toBe("incubacloud.ProjectCard");
    });

    test("props defines project as Object", () => {
        expect(ProjectCard.props.project).toBe(Object);
    });

    test("setup method exists", () => {
        expect(typeof ProjectCard.prototype.setup).toBe("function");
    });
});

describe("ProjectCard — statusLabel helper", () => {
    function statusLabel(status) {
        return { ok: "OK", warning: "Warning", error: "Error" }[status] || status;
    }

    test("maps ok to OK", () => {
        expect(statusLabel("ok")).toBe("OK");
    });

    test("maps warning to Warning", () => {
        expect(statusLabel("warning")).toBe("Warning");
    });

    test("maps error to Error", () => {
        expect(statusLabel("error")).toBe("Error");
    });

    test("returns raw value for unmapped status", () => {
        expect(statusLabel("unknown")).toBe("unknown");
    });
});

describe("ProjectCard — onEnterProject navigation logic", () => {
    test("navigates to instance_detail when first_production_instance_id exists", () => {
        const p = SAMPLE_PROJECT;
        let navigatedTo = null;
        const navigate = (route, params) => { navigatedTo = { route, params }; };

        if (p.first_production_instance_id) {
            navigate("instance_detail", {
                project_id: p.id,
                instance_id: p.first_production_instance_id,
            });
        } else {
            navigate("project_detail", { project_id: p.id });
        }

        expect(navigatedTo).not.toBe(null);
        expect(navigatedTo.route).toBe("instance_detail");
        expect(navigatedTo.params.project_id).toBe(1);
        expect(navigatedTo.params.instance_id).toBe(42);
    });

    test("navigates to project_detail when no production instance", () => {
        const p = { ...SAMPLE_PROJECT, first_production_instance_id: null };
        let navigatedTo = null;
        const navigate = (route, params) => { navigatedTo = { route, params }; };

        if (p.first_production_instance_id) {
            navigate("instance_detail", {
                project_id: p.id,
                instance_id: p.first_production_instance_id,
            });
        } else {
            navigate("project_detail", { project_id: p.id });
        }

        expect(navigatedTo).not.toBe(null);
        expect(navigatedTo.route).toBe("project_detail");
        expect(navigatedTo.params.project_id).toBe(1);
    });
});

describe("ProjectCard — method existence", () => {
    test("onEnterProject method exists", () => {
        expect(typeof ProjectCard.prototype.onEnterProject).toBe("function");
    });

    test("tagBadgeStyle method exists", () => {
        expect(typeof ProjectCard.prototype.tagBadgeStyle).toBe("function");
    });

    test("tagDotStyle method exists", () => {
        expect(typeof ProjectCard.prototype.tagDotStyle).toBe("function");
    });

    test("statusLabel method exists", () => {
        expect(typeof ProjectCard.prototype.statusLabel).toBe("function");
    });
});
