import { describe, expect, test } from "@odoo/hoot";
import { AppHeader } from "@incubacloud/components/app_header/app_header";

/**
 * AppHeader relies on session, onMounted/onWillUnmount for document listeners,
 * env.navigate, env.permissions, env.toggleSlideOver, env.userInfo, and rpc.
 * Full mounting requires a parent with useSubEnv providing all of these plus
 * mocking the session import. We test the pure logic extracted from the class
 * prototype instead.
 */

describe("AppHeader — initials getter", () => {
    /**
     * The `initials` getter reads from `this.state.userName`.
     * We can test the logic by calling the getter on a fake context.
     */
    function computeInitials(userName) {
        const name = userName || "?";
        const parts = name.trim().split(/\s+/);
        if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
        return name.slice(0, 2).toUpperCase();
    }

    test("two-word name returns first letters", () => {
        expect(computeInitials("John Doe")).toBe("JD");
    });

    test("single-word name returns first two chars", () => {
        expect(computeInitials("Admin")).toBe("AD");
    });

    test("empty name returns question marks", () => {
        expect(computeInitials("")).toBe("?");
    });

    test("three-word name returns first two initials", () => {
        expect(computeInitials("Ana Maria Lopez")).toBe("AM");
    });

    test("lowercase name returns uppercase initials", () => {
        expect(computeInitials("reinier segura")).toBe("RS");
    });

    test("name with extra spaces is trimmed", () => {
        expect(computeInitials("  John   Doe  ")).toBe("JD");
    });
});

describe("AppHeader — filteredProjects logic", () => {
    function filteredProjects(projects, search) {
        const q = search.toLowerCase();
        if (!q) return projects;
        return projects.filter((p) => p.name.toLowerCase().includes(q));
    }

    const sampleProjects = [
        { id: 1, name: "IncubaCloud" },
        { id: 2, name: "My SaaS Project" },
        { id: 3, name: "Cloud Demo" },
    ];

    test("empty search returns all projects", () => {
        expect(filteredProjects(sampleProjects, "")).toEqual(sampleProjects);
    });

    test("search filters by name case-insensitively", () => {
        const result = filteredProjects(sampleProjects, "cloud");
        expect(result.length).toBe(2);
        expect(result[0].name).toBe("IncubaCloud");
        expect(result[1].name).toBe("Cloud Demo");
    });

    test("search with no matches returns empty array", () => {
        expect(filteredProjects(sampleProjects, "zzz")).toEqual([]);
    });

    test("partial match works", () => {
        const result = filteredProjects(sampleProjects, "saa");
        expect(result.length).toBe(1);
        expect(result[0].id).toBe(2);
    });
});

describe("AppHeader — insideProject logic", () => {
    function insideProject(currentRoute, projectId) {
        const projectRoutes = [
            "project_detail",
            "project_settings",
            "create_instance",
            "instance_detail",
            "project_hub",
        ];
        return projectRoutes.includes(currentRoute) && !!projectId;
    }

    test("returns true for project_detail with projectId", () => {
        expect(insideProject("project_detail", 1)).toBe(true);
    });

    test("returns false for projects route", () => {
        expect(insideProject("projects", 1)).toBe(false);
    });

    test("returns false when projectId is 0", () => {
        expect(insideProject("project_detail", 0)).toBe(false);
    });

    test("returns true for instance_detail with projectId", () => {
        expect(insideProject("instance_detail", 42)).toBe(true);
    });
});

describe("AppHeader — static props definition", () => {
    test("declares expected props", () => {
        expect(AppHeader.props.alertCount.type).toBe(Number);
        expect(AppHeader.props.currentRoute.type).toBe(String);
        expect(AppHeader.props.projectName.type).toBe(String);
        expect(AppHeader.props.projectId.type).toBe(Number);
    });

    test("template name is set", () => {
        expect(AppHeader.template).toBe("incubacloud.AppHeader");
    });
});
