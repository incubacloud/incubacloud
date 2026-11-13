import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, waitFor } from "@odoo/hoot-dom";
import { advanceTime, animationFrame } from "@odoo/hoot-mock";
import {
    patchTranslations,
    mountWithCleanup,
    patchWithCleanup,
    preloadBundle,
} from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { REFRESH_MS } from "@incubacloud/components/metrics/chart_board";
import { HostBoard } from "@incubacloud/components/metrics/host_board";
import { FleetBoard } from "@incubacloud/components/metrics/fleet_board";
import { Monitoring } from "@incubacloud/components/monitoring/monitoring";
import { formatValue } from "@incubacloud/components/metrics/formatters";
import { relayIcon } from "@incubacloud/utils/icons";

/**
 * A server's board (its Metrics tab, Monitoring's Hosts view), the
 * fleet's board and Monitoring itself, drawn by the panel: no frame of
 * another tool.
 */

/**
 * A board stand-in of *Cls* holding the answer *board*, on the shipped
 * methods.
 *
 * @param {Function} Cls HostBoard or FleetBoard
 * @param {object} board the route's answer
 * @param {object} [props]
 * @returns {object}
 */
function boardWith(Cls, board, props = {}) {
    const comp = Object.create(Cls.prototype);
    comp.props = props;
    comp.state = { board, range: "1h", zoom: null };
    comp.env = { permissions: {} };
    return comp;
}

describe("HostBoard", () => {
    beforeEach(() => patchTranslations());

    test("reads its server's board in one route", () => {
        const comp = boardWith(HostBoard, null, { hostId: 3 });
        expect(comp.route).toBe("/cloud/host/resource_series");
        expect(comp.routeParams()).toEqual({ host_id: 3 });
    });

    test("every chart card names a panel the server answers", () => {
        const keys = ["load", "use", "disk", "traffic"].flatMap((section) =>
            boardWith(HostBoard, null).cardsOf(section).map((c) => c.key),
        );
        expect(keys).toEqual([
            "load", "pressure", "memory",
            "cpu_instances", "memory_instances",
            "root", "io", "io_bytes", "disk_instances",
            "network", "requests", "latency",
        ]);
    });

    test("an empty board says why, from the server's own facts", () => {
        const texts = {
            agents_missing: "are not installed",
            agents_failed: "could not be installed",
            agents_waiting: "have not sent anything yet",
            host_silent: "stopped sending metrics on",
            window: "Pick a longer range",
        };
        for (const [code, words] of Object.entries(texts)) {
            const comp = boardWith(HostBoard, { panels: {}, silence: { code, since: "2026-11-12T09:00:00+00:00" } });
            expect(comp.silenceText).toInclude(words);
        }
    });

    test("the ingestion line says how fresh the readings are", () => {
        const fresh = boardWith(HostBoard, { panels: { host: { value: { ingest_age: 12 } } } });
        expect(fresh.ingestText).toInclude("12 s");
        expect(fresh.ingestLate).toBe(false);
        const late = boardWith(HostBoard, { panels: { host: { value: {} } } });
        expect(late.ingestText).toInclude("No sample");
        expect(late.ingestLate).toBe(true);
    });

    test("a row of the table opens its instance only when the panel knows it", () => {
        const navigated = [];
        const comp = boardWith(HostBoard, null);
        comp.env = { navigate: (...args) => navigated.push(args) };
        comp.openInstance({ id: "9", project_id: 4 });
        comp.openInstance({ id: "10", project_id: false });
        expect(navigated).toEqual([["instance_detail", { project_id: 4, instance_id: 9 }]]);
        expect(comp.runText({ running: true })).toBe("running");
        expect(comp.runText({ running: null })).toBe("—");
    });

    test("what a stacked panel leaves out is named others", () => {
        const comp = boardWith(HostBoard, { panels: { cpu_instances: { series: [
            { name: "a", role: "use", points: [[1, 2]] },
            { name: "", role: "others", points: [[1, 3]] },
        ] } } });
        expect(comp.series("cpu_instances").map((s) => s.name)).toEqual(["a", "others"]);
    });

    test("throughput reads per second", () => {
        expect(formatValue("bytes_rate", 2048)).toBe("2 KiB/s");
    });

    test("a database's connections read against their ceiling, with its lock waits", () => {
        const comp = boardWith(HostBoard, null);
        expect(comp.connectionsText({ connections: 12, max_connections: 100 })).toBe("12 of 100");
        expect(comp.connectionsText({ connections: 12, max_connections: 100, waiting: 3 })).toBe("12 of 100 · 3 waiting");
        expect(comp.connectionsText({})).toBe("—");
    });

    test("a database collector the agents could not install is said, and only then", () => {
        const missing = boardWith(HostBoard, { panels: { host: { value: { pg_collector: 0 } } } });
        expect(missing.pgCollectorMissing).toBe(true);
        const installed = boardWith(HostBoard, { panels: { host: { value: { pg_collector: 1 } } } });
        expect(installed.pgCollectorMissing).toBe(false);
        const off = boardWith(HostBoard, { panels: { host: { value: {} } } });
        expect(off.pgCollectorMissing).toBe(false);
    });
});

describe("FleetBoard", () => {
    beforeEach(() => patchTranslations());

    test("another account's server carries its account on the operator's board", () => {
        const comp = boardWith(FleetBoard, { scope: "operator", panels: { load: { series: [
            { name: "mine", role: "use", own: true, account: "acct_a", host_id: "3", points: [[1, 1]] },
            { name: "theirs", role: "use", own: false, account: "acct_b", host_id: "3", points: [[1, 1]] },
        ] } } });
        expect(comp.series("load").map((s) => s.name)).toEqual(["mine", "theirs (acct_b)"]);
        expect(comp.scopeText).toBe("Every account on the central");
        expect(boardWith(FleetBoard, { scope: "account", panels: {} }).scopeText).toBe("Your servers");
    });

    test("a figure that needs a look takes a tone", () => {
        const comp = boardWith(FleetBoard, {
            panels: { summary: { value: { hosts: 4, disk_full: 1, memory_full: 0, pressure: 2 } } },
            alerts: { performance: 0, security: 3 },
        });
        const tones = Object.fromEntries(comp.tiles.map((t) => [t.key, t.tone]));
        expect(tones.hosts).toBe("ok");
        expect(tones.disk_full).toBe("bad");
        expect(tones.memory_full).toBe("ok");
        expect(tones.pressure).toBe("warn");
        expect(tones.alerts_security).toBe("bad");
        expect(comp.tiles.find((t) => t.key === "deployed").value).toBe("—");
    });

    test("a segment of the ingestion line sits where it happened", () => {
        const comp = boardWith(FleetBoard, { start: 0, end: 1000, panels: {} });
        expect(comp.segStyle({ start: 250, end: 500, state: "silent" }))
            .toBe("inset-inline-start:25.00%;inline-size:25.00%");
    });

    test("only a server of this panel opens, for whoever manages hosts", () => {
        const opened = [];
        const comp = boardWith(FleetBoard, null, { onOpenHost: (id) => opened.push(id) });
        expect(comp.canOpenHost({ own: true, host_id: "3" })).toBe(false);
        comp.env = { permissions: { can_manage_hosts: true } };
        expect(comp.canOpenHost({ own: true, host_id: "3" })).toBe(true);
        expect(comp.canOpenHost({ own: false, host_id: "3" })).toBe(false);
        comp.openHost({ host_id: "3" });
        expect(opened).toEqual([3]);
    });
});

/**
 * Answer every read with *answers[url]* and count them.
 *
 * @param {object} answers by route
 * @returns {{asked: object}}
 */
function answer(answers) {
    const asked = {};
    patchWithCleanup(rpc, {
        _rpc(url) {
            asked[url] = (asked[url] || 0) + 1;
            return Promise.resolve(answers[url]);
        },
    });
    return { asked };
}

const ENV = {
    icon: relayIcon,
    permissions: { can_manage_hosts: true, can_view_metrics: true },
    features: { observability: { configure: true } },
    navigate: () => {},
};

describe("Boards, mounted", () => {
    preloadBundle("web.chartjs_lib");
    beforeEach(() => patchTranslations());

    test("a server's board off says how to turn it on and stops asking", async () => {
        const { asked } = answer({
            "/cloud/host/resource_series": { ok: false, reason: "disabled", error: "x" },
        });
        await mountWithCleanup(HostBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect(".ic-mb-stub[data-reason=disabled] h3").toHaveText("Observability is not enabled");
        await advanceTime(REFRESH_MS * 2);
        expect(asked["/cloud/host/resource_series"]).toBe(1);
    });

    test("a chart of a server the central failed says so in its card", async () => {
        answer({
            "/cloud/host/resource_series": {
                ok: true, start: 0, end: 3600, step: 30, timing: {},
                panels: {
                    memory: { series: [{ name: "used", role: "used", points: [[0, 40], [30, 41]] }] },
                    io: { series: [], error: "The metrics central could not answer." },
                    consumers: { rows: [{ id: "9", name: "prod", project_id: 4, cpu: 12, memory: 1024, running: true,
                                          database: 2048, filestore: 4096, connections: 5, max_connections: 100, waiting: 1 }] },
                    host: { value: { cores: 4, ingest_age: 20, pg_collector: 0 } },
                },
                diagnosis: { current: true, tone: "ok", findings: [{ code: "normal", tone: "ok", text: "Normal." }] },
                silence: { code: "window", since: null },
            },
        });
        await mountWithCleanup(HostBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect("[data-panel=io] .ic-mb-panel-error").toHaveText("The metrics central could not answer.");
        expect("[data-panel=memory] canvas").toHaveCount(1);
        expect("[data-panel=consumers] tbody tr").toHaveCount(1);
        expect("[data-panel=consumers] a").toHaveText("prod");
        expect("[data-panel=consumers] td[data-waiting=yes]").toHaveText(/5 of 100 · 1 waiting/);
        expect("[data-panel=consumers] [data-pg-collector=missing]").toHaveCount(1);
        expect(".ic-mb-finding[data-code=normal]").toHaveCount(1);
    });

    test("a busy central keeps the last board, with a quiet line", async () => {
        const routes = {
            "/cloud/host/resource_series": {
                ok: true, start: 0, end: 3600, step: 30, timing: {},
                panels: {
                    memory: { series: [{ name: "used", role: "used", points: [[0, 40], [30, 41]] }] },
                    host: { value: { cores: 4, ingest_age: 20 } },
                },
                diagnosis: { current: true, tone: "ok", findings: [] },
                silence: { code: "window", since: null },
            },
        };
        const { asked } = answer(routes);
        await mountWithCleanup(HostBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect(".ic-mb-busy").toHaveCount(0);
        routes["/cloud/host/resource_series"] = {
            ok: false, reason: "busy", since: null, error: "The metrics central is busy.",
        };
        await advanceTime(REFRESH_MS);
        await animationFrame();
        expect(asked["/cloud/host/resource_series"]).toBe(2);
        expect("[data-panel=memory] canvas").toHaveCount(1);
        expect(".ic-mb-busy[role=status]").toHaveCount(1);
        expect(".ic-mb-stub").toHaveCount(0);
    });

    test("a busy central with no board yet says so, not as an error", async () => {
        answer({
            "/cloud/host/resource_series": {
                ok: false, reason: "busy", since: null, error: "The metrics central is busy.",
            },
        });
        await mountWithCleanup(HostBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect(".ic-mb-stub[data-reason=busy] h3").toHaveText("The metrics central is busy");
        expect(".ic-mb-stub[data-reason=busy]").toHaveAttribute("role", "status");
    });

    test("Monitoring draws the fleet, then a server, with no frame", async () => {
        const { asked } = answer({
            "/cloud/monitoring/config": { ok: true, hosts: [{ id: 3, name: "srv-01" }, { id: 5, name: "srv-02" }] },
            "/cloud/fleet/resource_series": {
                ok: true, scope: "account", start: 0, end: 3600, step: 30, timing: {},
                panels: { summary: { value: { hosts: 2 } }, ingest: { rows: [], hidden: 0 } },
                alerts: { performance: 0, security: 0 },
                silence: { code: "window" },
            },
            "/cloud/host/resource_series": { ok: false, reason: "central", since: null, error: "down" },
        });
        await mountWithCleanup(Monitoring, { env: ENV, noMainContainer: true });
        await animationFrame();
        expect("iframe").toHaveCount(0);
        expect("[data-tile=hosts] .ic-fb-tile-value").toHaveText("2");
        expect(".ic-fb-scope").toHaveText("Your servers");
        await click("button[data-view=hosts]");
        await animationFrame();
        expect(asked["/cloud/host/resource_series"]).toBe(1);
        // The answer renders once it arrives.
        await waitFor(".ic-mb-stub[data-reason=central]");
        expect(".ic-mb-stub[data-reason=central]").toHaveCount(1);
    });
});
