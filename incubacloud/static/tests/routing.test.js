import { describe, expect, test } from "@odoo/hoot";
import { _parseRoute, _buildUrl } from "@incubacloud/app/app";

describe("_parseRoute", () => {
    test("root path returns projects", () => {
        const { route, params } = _parseRoute("/cloud");
        expect(route).toBe("projects");
        expect(params).toEqual({});
    });

    test("/cloud/projects returns projects", () => {
        const { route, params } = _parseRoute("/cloud/projects");
        expect(route).toBe("projects");
        expect(params).toEqual({});
    });

    test("/cloud/projects/123 returns project_detail", () => {
        const { route, params } = _parseRoute("/cloud/projects/123");
        expect(route).toBe("project_detail");
        expect(params).toEqual({ project_id: 123 });
    });

    test("/cloud/projects/123/settings returns project_settings", () => {
        const { route, params } = _parseRoute("/cloud/projects/123/settings");
        expect(route).toBe("project_settings");
        expect(params).toEqual({ project_id: 123 });
    });

    test("/cloud/projects/123/instances/456 returns instance_detail", () => {
        const { route, params } = _parseRoute("/cloud/projects/123/instances/456");
        expect(route).toBe("instance_detail");
        expect(params).toEqual({ project_id: 123, instance_id: 456 });
    });

    test("/cloud/projects/123/instances/new returns create_instance", () => {
        const { route, params } = _parseRoute("/cloud/projects/123/instances/new");
        expect(route).toBe("create_instance");
        expect(params.project_id).toBe(123);
    });

    test("/cloud/hosts returns hosts", () => {
        const { route, params } = _parseRoute("/cloud/hosts");
        expect(route).toBe("hosts");
        expect(params).toEqual({});
    });

    test("/cloud/hosts/5 returns host_detail", () => {
        const { route, params } = _parseRoute("/cloud/hosts/5");
        expect(route).toBe("host_detail");
        expect(params).toEqual({ host_id: 5 });
    });

    test("/cloud/hosts/new returns new_host", () => {
        const { route } = _parseRoute("/cloud/hosts/new");
        expect(route).toBe("new_host");
    });

    test("/cloud/settings returns settings", () => {
        const { route, params } = _parseRoute("/cloud/settings");
        expect(route).toBe("settings");
        expect(params).toEqual({});
    });

    test("/cloud/backup-backends returns backup_backends", () => {
        const { route, params } = _parseRoute("/cloud/backup-backends");
        expect(route).toBe("backup_backends");
        expect(params).toEqual({});
    });

    test("/cloud/backup-backends/7 returns backup_backend_detail", () => {
        const { route, params } = _parseRoute("/cloud/backup-backends/7");
        expect(route).toBe("backup_backend_detail");
        expect(params).toEqual({ backend_id: 7 });
    });

    test("/cloud/invalid falls back to projects", () => {
        const { route, params } = _parseRoute("/cloud/invalid");
        expect(route).toBe("projects");
        expect(params).toEqual({});
    });
});

describe("_buildUrl", () => {
    test("projects route builds /cloud/projects", () => {
        expect(_buildUrl("projects", {})).toBe("/cloud/projects");
    });

    test("project_detail builds /cloud/projects/:id", () => {
        expect(_buildUrl("project_detail", { project_id: 42 })).toBe("/cloud/projects/42");
    });

    test("instance_detail builds /cloud/projects/:pid/instances/:iid", () => {
        expect(_buildUrl("instance_detail", { project_id: 1, instance_id: 2 })).toBe(
            "/cloud/projects/1/instances/2"
        );
    });

    test("hosts route builds /cloud/hosts", () => {
        expect(_buildUrl("hosts", {})).toBe("/cloud/hosts");
    });

    test("host_detail builds /cloud/hosts/:id", () => {
        expect(_buildUrl("host_detail", { host_id: 10 })).toBe("/cloud/hosts/10");
    });

    test("settings route builds /cloud/settings", () => {
        expect(_buildUrl("settings", {})).toBe("/cloud/settings");
    });
});
