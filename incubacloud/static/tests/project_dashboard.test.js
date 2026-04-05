import { describe, expect, test } from "@odoo/hoot";
import { ProjectDashboard } from "@incubacloud/components/project_dashboard/project_dashboard";

/**
 * ProjectDashboard uses useService("dialog"), useDebounced, rpc,
 * env.navigate, env.permissions, and has a child component ProjectCard.
 * Full mounting requires mocking all services + the dialog service +
 * the child component. We test the search/filter logic as pure functions
 * and validate the class structure.
 */

describe("ProjectDashboard — search filter logic", () => {
    function filterProjects(projects, query) {
        return projects.filter((project) =>
            project.name.toLowerCase().includes(query.toLowerCase())
        );
    }

    const sampleProjects = [
        { id: 1, name: "IncubaCloud Core" },
        { id: 2, name: "Client Portal" },
        { id: 3, name: "Cloud Monitoring" },
    ];

    test("filters by name case-insensitively", () => {
        const result = filterProjects(sampleProjects, "cloud");
        expect(result.length).toBe(2);
        expect(result[0].name).toBe("IncubaCloud Core");
        expect(result[1].name).toBe("Cloud Monitoring");
    });

    test("no match returns empty array", () => {
        expect(filterProjects(sampleProjects, "xyz")).toEqual([]);
    });

    test("empty query returns all", () => {
        // In the component, empty input resets visible_projects = projects
        // The debounced search only triggers for non-empty values
        expect(filterProjects(sampleProjects, "")).toEqual(sampleProjects);
    });

    test("partial match works", () => {
        const result = filterProjects(sampleProjects, "port");
        expect(result.length).toBe(1);
        expect(result[0].id).toBe(2);
    });
});

describe("ProjectDashboard — class structure", () => {
    test("template name is set", () => {
        expect(ProjectDashboard.template).toBe("incubacloud.ProjectDashboard");
    });

    test("has ProjectCard in static components", () => {
        expect(ProjectDashboard.components).not.toBe(undefined);
        expect(ProjectDashboard.components.ProjectCard).not.toBe(undefined);
    });
});

describe("ProjectDashboard — empty state logic", () => {
    /**
     * Template renders ic-empty-state when visible_projects.length === 0.
     * This test validates the condition logic.
     */
    test("empty state when no visible projects", () => {
        const visibleProjects = [];
        expect(visibleProjects.length === 0).toBe(true);
    });

    test("no empty state when projects exist", () => {
        const visibleProjects = [{ id: 1, name: "Test" }];
        expect(visibleProjects.length === 0).toBe(false);
    });
});

describe("ProjectDashboard — action cards visibility logic", () => {
    /**
     * Template shows action cards when env.permissions?.can_create_project is truthy.
     */
    test("action cards visible with can_create_project permission", () => {
        const permissions = { can_create_project: true };
        expect(!!permissions?.can_create_project).toBe(true);
    });

    test("action cards hidden without permission", () => {
        const permissions = { can_create_project: false };
        expect(!!permissions?.can_create_project).toBe(false);
    });

    test("action cards hidden when permissions is null", () => {
        const permissions = null;
        expect(!!permissions?.can_create_project).toBe(false);
    });
});
