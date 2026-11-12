import { describe, expect, test } from "@odoo/hoot";
import { Component } from "@odoo/owl";
import { Monitoring } from "@incubacloud/components/monitoring/monitoring";

/**
 * Monitoring draws the fleet's board and one server's, as the panel's
 * own charts (no frame of another tool since 12-nov-2026). The mounted
 * page is tested with the boards, in host_fleet_board.test.js.
 */
describe("Monitoring — class structure", () => {
    test("is a Component with the right template, no props and its two boards", () => {
        expect(Monitoring.prototype).toBeInstanceOf(Component);
        expect(Monitoring.template).toBe("incubacloud.Monitoring");
        expect(Monitoring.props).toEqual({});
        expect(Object.keys(Monitoring.components)).toEqual(["SearchSelect", "FleetBoard", "HostBoard"]);
        expect(Monitoring.DASHBOARDS).toBe(undefined);
    });
});

describe("Monitoring — who may configure it", () => {
    /**
     * The axis that decides whether "set it up in Settings" is advice or
     * a dead end. It comes from the boot config rather than the metrics
     * route because it is a property of the deployment, not a setting.
     */
    const withEnv = (features) => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.env = { features };
        return cmp;
    };

    test("a panel that owns its settings may be told to open them", () => {
        expect(withEnv({ observability: { configure: true } }).canConfigure)
            .toBe(true);
    });

    test("a panel whose settings are injected may not", () => {
        // The managed-panel case: pointing them at a Settings tab their panel
        // hides is the dead end this replaces.
        expect(withEnv({ observability: { configure: false } }).canConfigure)
            .toBe(false);
    });

    test("defaults to configurable when the descriptor is absent", () => {
        // A boot config that failed to load must not silently downgrade
        // an operator's panel into the injected-settings variant.
        expect(withEnv(undefined).canConfigure).toBe(true);
        expect(withEnv({}).canConfigure).toBe(true);
    });
});

describe("Monitoring — host picker", () => {
    test("shows the name and sends the id", () => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = { hosts: [{ id: 3, name: "srv-01" }], host: "3" };
        expect(cmp.hostOptions).toEqual([{ value: "3", label: "srv-01" }]);
        expect(cmp.hostId).toBe(3);
        cmp.state.host = "";
        expect(cmp.hostId).toBe(0);
    });

    test("a server of the fleet opens in the Hosts view, if the panel lists it", () => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = { hosts: [{ id: 3, name: "srv-01" }], host: "", view: "fleet" };
        cmp.openHost(7);
        expect(cmp.state.view).toBe("fleet");
        cmp.openHost(3);
        expect(cmp.state).toEqual({ hosts: [{ id: 3, name: "srv-01" }], host: "3", view: "hosts" });
    });
});

describe("Monitoring — a fleet with no server", () => {
    /**
     * With no server nothing can report, and every chart read "No data"
     * as if the page were broken (2-nov-2026, a managed panel without a server).
     * The page says why instead of drawing empty boards.
     */
    const withHosts = (hosts) => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = { hosts };
        return cmp;
    };

    test("is told apart from a fleet that has one", () => {
        expect(withHosts([]).hasHosts).toBe(false);
        expect(withHosts([{ id: 1, name: "vps-1" }]).hasHosts).toBe(true);
    });
});
