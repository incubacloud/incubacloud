import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, waitFor } from "@odoo/hoot-dom";
import { advanceTime, animationFrame } from "@odoo/hoot-mock";
import {
    allowTranslations,
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
    beforeEach(() => allowTranslations());

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

    test("load, waiting and memory share a row: none of them takes a row of its own", () => {
        expect(boardWith(HostBoard, null).cardsOf("load").some((c) => c.wide)).toBe(false);
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
    beforeEach(() => allowTranslations());

    test("every line of a legend is a server by its name, and the board is the panel's own", () => {
        const comp = boardWith(FleetBoard, { panels: { load: { series: [
            { name: "srv-01", role: "use", own: true, host_id: "3", points: [[1, 1]] },
            { name: "Server 9", role: "use", own: false, host_id: "9", points: [[1, 1]] },
        ] } } });
        expect(comp.series("load").map((s) => s.name)).toEqual(["srv-01", "Server 9"]);
        expect(comp.scopeText).toBe("Your servers");
        // An answer that says "operator", as before 17-nov-2026, changes nothing.
        expect(boardWith(FleetBoard, { scope: "operator", panels: {} }).scopeText).toBe("Your servers");
    });

    test("the four charts come with the ingestion line's card after them", () => {
        expect(boardWith(FleetBoard, { panels: {} }).cards.map((c) => c.key)).toEqual(["load", "memory", "root", "io"]);
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

    test("the Security figures take a tone, the central's and the panel's", () => {
        const comp = boardWith(FleetBoard, {
            panels: { security: { value: { ssh_failed_1h: 340, ssh_bruteforce: 1, bans_1h: 5,
                weblogin_bruteforce: 0, silent: 0 } } },
            security: { state: "ready", off: 2, unhardened: 0, alerts_hosts: 1, rows: [], hidden: 0 },
        });
        const tiles = Object.fromEntries(comp.securityTiles.map((t) => [t.key, t]));
        expect(tiles.ssh_failed_1h.tone).toBe("ok");
        expect(tiles.ssh_bruteforce.tone).toBe("warn");
        expect(tiles.weblogin_bruteforce.tone).toBe("ok");
        expect(tiles.alerts_hosts.tone).toBe("bad");
        expect(tiles.off.value).toBe("2");
        expect(tiles.unhardened.tone).toBe("ok");
        expect(boardWith(FleetBoard, { panels: {}, security: {} }).securityTiles[0].value).toBe("—");
    });

    test("a server of the Security block says what to look at, worst first", () => {
        const comp = boardWith(FleetBoard, { panels: {} });
        const flags = comp.securityFlags({
            alerts: 2, level: "critical", ssh_bruteforce: 340, weblogin_bruteforce: 1,
            silent: true, off: false, unhardened: true,
        });
        expect(flags.map((f) => f.key)).toEqual([
            "alerts", "ssh_bruteforce", "weblogin_bruteforce", "silent", "unhardened",
        ]);
        expect(flags[0].tone).toBe("bad");
        expect(flags[0].text).toBe("Security alerts: 2");
        expect(comp.securityFlags({ alerts: 1, level: "warning" })[0].tone).toBe("warn");
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
    beforeEach(() => allowTranslations());

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

    test("the fleet's Security block lists this panel's servers, with a link for a manager", async () => {
        const opened = [];
        answer({
            "/cloud/fleet/resource_series": {
                ok: true, start: 0, end: 3600, step: 30, timing: {},
                panels: {
                    summary: { value: { hosts: 3 } }, ingest: { rows: [], hidden: 0 },
                    security: { value: { ssh_failed_1h: 340, ssh_bruteforce: 2, bans_1h: 5,
                        weblogin_bruteforce: 3, silent: 1 } },
                    security_ssh: { series: [] },
                },
                alerts: { performance: 0, security: 1 },
                silence: { code: "window" },
                security: { state: "ready", off: 1, unhardened: 0, alerts_hosts: 1, hidden: 0, rows: [
                    { host_id: 3, name: "srv-01", own: true, off: false, unhardened: false,
                        alerts: 1, level: "critical", ssh_bruteforce: 340 },
                    { host_id: 5, name: "srv-02", own: true, off: true, unhardened: false },
                ] },
            },
        });
        await mountWithCleanup(FleetBoard, {
            props: { onOpenHost: (id) => opened.push(id) }, env: ENV, noMainContainer: true,
        });
        await animationFrame();
        expect("[data-security=ready] [data-tile=ssh_bruteforce] .ic-fb-tile-value").toHaveText("2");
        expect("[data-security=ready] [data-tile=off] .ic-fb-tile-value").toHaveText("1");
        expect("[data-note=operator]").toHaveCount(0);
        expect(".ic-fb-sec-row").toHaveCount(2);
        expect(".ic-fb-sec-row[data-host='3'] [data-flag=alerts]").toHaveClass("is-bad");
        expect(".ic-fb-sec-row[data-host='5'] [data-flag=off]").toHaveText("Security logs off");
        expect("[data-panel=security_ssh] .ic-mb-empty").toHaveText("No failed SSH login on the servers in this window.");
        await click(".ic-fb-sec-row[data-host='3'] a");
        expect(opened).toEqual([3]);
    });

    test("the ingestion line is a card of the charts' grid, each server by its name", async () => {
        const opened = [];
        const flat = [[0, 1], [30, 1]];
        const line = (name, host_id, own) => ({ name, role: "use", own, host_id, points: flat });
        answer({
            "/cloud/fleet/resource_series": {
                ok: true, start: 0, end: 3600, step: 30, timing: {},
                panels: {
                    summary: { value: { hosts: 2 } },
                    load: { series: [line("srv-01", "3", true), line("Server 9", "9", false)] },
                    ingest: { hidden: 0, rows: [
                        { name: "srv-01", host_id: "3", own: true, segments: [{ start: 0, end: 3600, state: "ok" }] },
                        { name: "Server 9", host_id: "9", own: false, segments: [
                            { start: 0, end: 1800, state: "ok" }, { start: 1800, end: 3600, state: "silent" },
                        ] },
                    ] },
                },
                alerts: { performance: 0, security: 0 },
                silence: { code: "window" },
            },
        });
        await mountWithCleanup(FleetBoard, {
            props: { onOpenHost: (id) => opened.push(id) }, env: ENV, noMainContainer: true,
        });
        await animationFrame();
        // One grid: the four charts, then the line, which fills the row.
        expect(".ic-mb-fill > [data-panel]").toHaveCount(5);
        expect(".ic-mb-fill > [data-panel]:last-child").toHaveAttribute("data-panel", "ingest");
        expect(".ic-mb-fill > [data-panel=ingest]").toHaveClass("ic-mb-span2");
        expect("[data-panel=ingest] .rl-ovtitle").toHaveText(/are the servers reporting\?/i);
        expect("[data-panel=ingest] .ic-fb-tl-row").toHaveCount(2);
        expect("[data-panel=ingest] .ic-fb-tl-row:first-child .ic-fb-tl-name").toHaveText("srv-01");
        expect("[data-panel=ingest] .ic-fb-tl-seg.is-silent").toHaveCount(1);
        expect("[data-panel=ingest]").not.toHaveText(/acct_/);
        // Only the server the reader can open links to its board.
        expect("[data-panel=ingest] a").toHaveCount(1);
        await click("[data-panel=ingest] a");
        expect(opened).toEqual([3]);
    });

    test("the fleet's Security block of a panel without the permission says so alone", async () => {
        answer({
            "/cloud/fleet/resource_series": {
                ok: true, start: 0, end: 3600, step: 30, timing: {},
                panels: { summary: { value: { hosts: 1 } }, ingest: { rows: [], hidden: 0 } },
                alerts: { performance: 0, security: 0 },
                silence: { code: "window" },
                security: { state: "unavailable" },
            },
        });
        await mountWithCleanup(FleetBoard, { env: { ...ENV, permissions: { can_view_metrics: true } }, noMainContainer: true });
        await animationFrame();
        expect("[data-security=unavailable] .ic-fb-sec-title").toHaveText("Security logs are not available on this panel yet");
        expect("[data-security=unavailable] [data-panel=security]").toHaveCount(0);
        expect(".ic-fb-sec-row").toHaveCount(0);
    });

    test("Monitoring draws the fleet, then a server, with no frame", async () => {
        const { asked } = answer({
            "/cloud/monitoring/config": { ok: true, hosts: [{ id: 3, name: "srv-01" }, { id: 5, name: "srv-02" }] },
            "/cloud/fleet/resource_series": {
                ok: true, start: 0, end: 3600, step: 30, timing: {},
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
