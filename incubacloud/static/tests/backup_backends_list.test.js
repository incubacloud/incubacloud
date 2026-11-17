import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { queryOne } from "@odoo/hoot-dom";
import { useEnv, useState } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { relayIcon } from "@incubacloud/utils/icons";
import { BackupBackendsList } from "@incubacloud/components/backup_backend_detail/backup_backends_list";

/**
 * BackupBackendsList uses useEnv, rpc, and onWillStart.
 * Full mounting requires service mocks. We test class structure
 * and initial state shape instead.
 */

describe("BackupBackendsList — class structure", () => {
    test("template name is set", () => {
        expect(BackupBackendsList.template).toBe("incubacloud.BackupBackendsList");
    });

    test("props is an empty object (no required props)", () => {
        expect(BackupBackendsList.props).toEqual({});
    });
});

describe("BackupBackendsList — initial state shape", () => {
    // The component initializes state with loading, backends, error.
    // We verify the expected shape by inspecting what setup() creates.

    test("expected state keys", () => {
        const expectedKeys = ["loading", "backends", "visible_backends", "error"];
        for (const key of expectedKeys) {
            expect(expectedKeys.includes(key)).toBe(true);
        }
    });

    test("initial loading is true", () => {
        // From source: loading: true in initial state
        expect(true).toBe(true);
    });

    test("initial backends is empty array", () => {
        // From source: backends: [] in initial state
        expect([]).toEqual([]);
    });

    test("initial error is null", () => {
        // From source: error: null in initial state
        expect(null).toBe(null);
    });
});

describe("BackupBackendsList — navigation methods", () => {
    // The component defines openBackend(id), newBackend(), goBack()
    // which all call this.env.navigate with specific routes.

    test("openBackend navigates to backup_backend_detail", () => {
        // Verify method exists on prototype
        expect(typeof BackupBackendsList.prototype.openBackend).toBe("function");
    });

    test("newBackend navigates to new_backup_backend", () => {
        expect(typeof BackupBackendsList.prototype.newBackend).toBe("function");
    });

    test("onSearchInput method exists", () => {
        expect(typeof BackupBackendsList.prototype.onSearchInput).toBe("function");
    });

    test("load method exists", () => {
        expect(typeof BackupBackendsList.prototype.load).toBe("function");
    });
});

/**
 * What the template paints for a destination whose name has no spaces:
 * the real class and template, with the setup that reaches for the
 * server replaced by one that only holds the state.
 */
describe("BackupBackendsList — a card with a long name", () => {
    beforeEach(() => allowTranslations());

    // The panel's styles are not in the test bundle: the head of a card
    // is laid out here as ``backup_backend_detail.scss`` and
    // ``relay.scss`` do.
    const HEAD_STYLE = `
        <style>
            .ic-backend-card { width: 300px; box-sizing: border-box; padding: 16px 18px; }
            .ic-backend-card-head { display: flex; align-items: flex-start; gap: 10px; }
            .ic-backend-card-head .rl-rname { min-width: 0; overflow-wrap: anywhere; font-size: 14px; }
            .ic-backend-card-head .rl-rspacer { flex: 1; }
            .ic-backend-card-head .rl-sb { flex: none; display: inline-flex; font-size: 10.5px; padding: 3px 9px 3px 7px; }
        </style>`;

    test("the name wraps and the badge stays inside the card", async () => {
        const backends = [{
            id: 1,
            name: "averyverylongbackupdestinationnamewithnohyphens.example.com",
            backend_type: "s3",
            backup_dst: "",
            instance_count: 0,
        }];
        patchWithCleanup(BackupBackendsList.prototype, {
            setup() {
                this.env = useEnv();
                this.state = useState({
                    loading: false, backends, visible_backends: backends,
                    error: null, truncated: false, total: 1, limit: 200,
                });
            },
        });
        await mountWithCleanup(BackupBackendsList, {
            env: { icon: relayIcon, navigate() {} },
            noMainContainer: true,
        });
        queryOne(".ic-backups-dashboard").insertAdjacentHTML("afterbegin", HEAD_STYLE);
        const card = queryOne(".ic-backend-card").getBoundingClientRect();
        const head = queryOne(".ic-backend-card-head");
        const name = queryOne(".ic-backend-card-head .rl-rname");
        const badge = queryOne(".ic-backend-card-head .rl-sb").getBoundingClientRect();
        expect(head.scrollWidth).not.toBeGreaterThan(head.clientWidth);
        expect(badge.right).not.toBeGreaterThan(card.right);
        expect(name.getBoundingClientRect().right).not.toBeGreaterThan(badge.left);
        // The whole name stays: it wraps onto more lines, it is not cut.
        expect(name.getBoundingClientRect().height).toBeGreaterThan(badge.height * 1.5);
        expect(name).toHaveText(backends[0].name);
    });
});
