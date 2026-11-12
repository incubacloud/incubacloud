import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, waitFor } from "@odoo/hoot-dom";
import { advanceTime, animationFrame } from "@odoo/hoot-mock";
import { Component, useState, xml } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
    preloadBundle,
} from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { MetricsBoard, REFRESH_MS } from "@incubacloud/components/metrics/metrics_board";
import { OnDemandPanel } from "@incubacloud/components/metrics/on_demand_panel";
import {
    TimeSeriesChart,
    timeUnit,
    toChartPoints,
} from "@incubacloud/components/metrics/time_series_chart";
import {
    formatBytes,
    formatDuration,
    formatPercent,
    formatSeconds,
    formatValue,
} from "@incubacloud/components/metrics/formatters";
import { relayIcon } from "@incubacloud/utils/icons";
import { useSafePoll } from "@incubacloud/utils/use_safe_poll";

/**
 * The instance's Metrics tab, drawn by the panel (no Grafana frame).
 *
 * The board, its charts and the readings on demand are tested on the
 * shipped methods; only the readings are mounted, as they are what a
 * person presses during an incident and they must not run two at once.
 */

describe("Metrics formatters", () => {
    beforeEach(() => allowTranslations());

    test("bytes in binary units, seconds in ms below one", () => {
        expect(formatBytes(512 * 1024 * 1024)).toBe("512\u00a0MiB");
        expect(formatBytes(1.5 * 1024 ** 3)).toBe("1.5\u00a0GiB");
        expect(formatSeconds(0.25)).toBe("250\u00a0ms");
        expect(formatSeconds(4.8)).toBe("4.8\u00a0s");
        expect(formatPercent(0.5)).toBe("0.5\u00a0%");
        expect(formatPercent(98.4)).toBe("98\u00a0%");
    });

    test("an age reads as minutes and seconds", () => {
        expect(formatDuration(45)).toBe("45 s");
        expect(formatDuration(372)).toBe("6 min 12 s");
        expect(formatDuration(7500)).toBe("2 h 5 min");
        expect(formatDuration(null)).toBe("—");
    });

    test("a panel's unit picks the formatter", () => {
        expect(formatValue("bytes", 1024)).toBe("1\u00a0KiB");
        expect(formatValue("seconds", 2)).toBe("2\u00a0s");
        expect(formatValue("rps", 12)).toBe("12 req/s");
    });
});

describe("TimeSeriesChart helpers", () => {
    beforeEach(() => allowTranslations());

    test("a gap of more than two steps stays a gap", () => {
        const points = toChartPoints([[0, 1], [30, 2], [300, 3]], 30);
        expect(points.map((p) => p.y)).toEqual([1, 2, null, 3]);
        expect(points[2].x).toBe(60 * 1000);
    });

    test("the time axis unit follows the span", () => {
        expect(timeUnit(3600 * 1000)).toBe("minute");
        expect(timeUnit(24 * 3600 * 1000)).toBe("hour");
        expect(timeUnit(7 * 24 * 3600 * 1000)).toBe("day");
    });

    test("services keep their colour, the instance asked about is green", () => {
        const chart = Object.create(TimeSeriesChart.prototype);
        chart.props = { palette: "service", highlightId: "", step: 30, series: [
            { name: "odoo", role: "use", points: [] },
            { name: "odoo", role: "limit", points: [] },
            { name: "redis", role: "use", points: [] },
        ] };
        const sets = chart.datasets();
        expect(sets[0].icToken).toBe("--rl-green");
        // The quota is the same colour, dashed.
        expect(sets[1].icToken).toBe("--rl-green");
        expect(sets[1].borderDash).toEqual([6, 4]);
        expect(sets[2].icToken).toBe("--rl-tag-6");

        chart.props = { palette: "neighbours", highlightId: "7", step: 30, series: [
            { name: "other", role: "use", id: "9", points: [] },
            { name: "mine", role: "use", id: "7", points: [] },
        ] };
        const [other, mine] = chart.datasets();
        expect(mine.icToken).toBe("--rl-green");
        expect(mine.borderWidth).toBeGreaterThan(other.borderWidth);
        expect(other.icToken).not.toBe("--rl-green");
    });
});

/**
 * A board stand-in holding the answer *board*, on the shipped methods.
 *
 * @param {object} board ``/cloud/instance/resource_series``'s answer
 * @returns {object}
 */
function boardWith(board) {
    const comp = Object.create(MetricsBoard.prototype);
    comp.props = { instanceId: 7 };
    comp.state = { board, range: "1h", zoom: null };
    return comp;
}

describe("MetricsBoard", () => {
    beforeEach(() => allowTranslations());

    test("is drawn by the panel, with no frame of another tool", () => {
        expect(MetricsBoard.template).toBe("incubacloud.MetricsBoard");
        expect(Object.keys(MetricsBoard.components)).toEqual([
            "TimeSeriesChart", "OnDemandPanel",
        ]);
        expect(REFRESH_MS).toBe(45000);
    });

    test("lines drawn by role get words in the legend", () => {
        const comp = boardWith({ panels: { latency: { series: [
            { name: "", role: "p95", points: [[1, 2]] },
            { name: "", role: "5xx", points: [] },
        ] } } });
        expect(comp.series("latency").map((s) => s.label)).toEqual(["p95", "errors (5xx)"]);
        expect(comp.hasSeries("latency")).toBe(true);
        expect(comp.hasData).toBe(true);
        expect(boardWith({ panels: { cpu: { series: [] } } }).hasData).toBe(false);
    });

    test("the diagnosis heads with what is short", () => {
        const warn = boardWith({ panels: {}, diagnosis: { tone: "warn", findings: [], facts: {} } });
        expect(warn.diagnosisHeading).toBe("Something is short");
        const ok = boardWith({ panels: {}, diagnosis: { tone: "ok", findings: [], facts: {} } });
        expect(ok.diagnosisHeading).toBe("Nothing looks saturated");
    });

    test("the workers line says how busy they are", () => {
        const comp = boardWith({
            panels: {},
            workers: { configured: 4 },
            diagnosis: { facts: { occupancy: 98 } },
        });
        expect(comp.workersText).toInclude("4 workers configured");
        expect(comp.workersText).toInclude("98\u00a0%");
        expect(boardWith({ panels: {}, workers: { configured: 0 } }).workersText)
            .toInclude("threaded mode");
    });

    test("clicks do not stack refreshes: one chain of 45 s at a time", async () => {
        let asked = 0;
        patchWithCleanup(rpc, {
            _rpc() {
                asked += 1;
                return Promise.resolve({ ok: true, panels: {}, timing: {} });
            },
        });
        let unmount;
        const comp = boardWith(null);
        comp.poll = useSafePoll({ unmountHook: (fn) => { unmount = fn; } });
        comp.timer = null;
        // Three clicks, each answered before the next.
        comp.setRange("1h");
        await animationFrame();
        comp.setRange("6h");
        await animationFrame();
        comp.zoomTo(1000, 2000);
        await animationFrame();
        comp.resetZoom();
        await animationFrame();
        expect(asked).toBe(4);
        await advanceTime(REFRESH_MS);
        expect(asked).toBe(5);
        await advanceTime(REFRESH_MS);
        expect(asked).toBe(6);
        unmount();
        await advanceTime(REFRESH_MS);
        expect(asked).toBe(6);
    });

    test("the database line and who blocks whom", () => {
        const comp = boardWith(null);
        const line = comp.dbSummary({
            connections: 48, max_connections: 100, active: 5, waiting_lock: 3,
            longest_active: 372, idle_in_transaction: 2, cache_hit_ratio: 99.1,
            size_bytes: 7.4 * 1024 ** 3,
        });
        expect(line).toInclude("48/100 connections");
        expect(line).toInclude("3 waiting on a lock");
        expect(line).toInclude("6 min 12 s");
        expect(line).toInclude("7.4\u00a0GiB");
        expect(comp.blockText({ blocked_by: [791], blocks: [] })).toBe("waits for 791");
        expect(comp.blockText({ blocked_by: [], blocks: [812, 813] })).toBe("blocks 812, 813");
        expect(comp.waitText({ wait_event_type: "Lock", wait_event: "transactionid" }))
            .toBe("Lock · transactionid");
    });

    test("an empty board says why, from the server's own facts", () => {
        const texts = {
            agents_missing: "are not installed",
            agents_failed: "could not be installed",
            agents_waiting: "have not sent anything yet",
            host_silent: "stopped sending metrics on",
            stopped: "This instance is stopped",
            new: "has not reported yet",
            window: "Pick a longer range",
        };
        for (const [code, words] of Object.entries(texts)) {
            const comp = boardWith({
                panels: {}, silence: { code, since: "2026-11-12T09:00:00+00:00" },
            });
            comp.env = { permissions: { can_manage_hosts: true } };
            expect(comp.silenceText).toInclude(words);
            // Where to look next, only about the server and only for
            // whoever can open Hosts.
            const aboutServer = code.startsWith("agents") || code === "host_silent";
            expect(!!comp.silenceHint).toBe(aboutServer);
            comp.env = { permissions: {} };
            expect(comp.silenceHint).toBe("");
        }
    });
});

/**
 * Mount the board on the answer *res* of every read, and count them.
 *
 * @param {object} res ``/cloud/instance/resource_series``'s answer
 * @param {object} [options]
 * @param {boolean} [options.canConfigure] the panel sets observability up
 * @param {object} [options.permissions] ``env.permissions``
 * @returns {Promise<{asked: () => number, navigated: Array}>}
 */
async function mountBoard(res, { canConfigure = true, permissions = {} } = {}) {
    let asked = 0;
    const navigated = [];
    patchWithCleanup(rpc, {
        _rpc(url) {
            if (url === "/cloud/instance/resource_series") {
                asked += 1;
            }
            return Promise.resolve(res);
        },
    });
    await mountWithCleanup(MetricsBoard, {
        props: { instanceId: 7, canConfigure },
        env: {
            icon: relayIcon,
            permissions,
            navigate: (...args) => navigated.push(args),
        },
        noMainContainer: true,
    });
    await animationFrame();
    return { asked: () => asked, navigated };
}

describe("MetricsBoard, mounted", () => {
    preloadBundle("web.chartjs_lib");
    beforeEach(() => allowTranslations());

    test("off: says how to turn it on, and opens Settings", async () => {
        const { navigated } = await mountBoard(
            { ok: false, reason: "disabled", error: "Observability is not enabled." },
            { permissions: { can_manage_settings: true } },
        );
        expect(".ic-mb-stub h3").toHaveText("Observability is not enabled");
        expect(".ic-mb-stub").toHaveText(/Where the central runs/);
        await click(".ic-mb-stub button");
        expect(navigated).toEqual([["settings", { section: "monitoring" }]]);
    });

    test("on without a central: says the central is missing and stops asking", async () => {
        const { asked } = await mountBoard(
            { ok: false, reason: "unconfigured", error: "No metrics central is configured." },
            { permissions: { can_manage_settings: true } },
        );
        expect(".ic-mb-stub h3").toHaveText("Observability is on, but there is no metrics central");
        expect(".ic-mb-stub").not.toHaveText(/not enabled/);
        expect(".ic-mb-stub button").toHaveText("Open Settings › Monitoring");
        expect(asked()).toBe(1);
        await advanceTime(REFRESH_MS * 2);
        expect(asked()).toBe(1);
    });

    test("no button to Settings for whoever cannot open them", async () => {
        await mountBoard({ ok: false, reason: "disabled", error: "x" });
        expect(".ic-mb-stub h3").toHaveCount(1);
        expect(".ic-mb-stub button").toHaveCount(0);
    });

    test("a panel that does not configure gets words, not a blank", async () => {
        for (const reason of ["disabled", "unconfigured"]) {
            await mountBoard(
                { ok: false, reason, error: "x" },
                { canConfigure: false, permissions: { can_manage_settings: true } },
            );
            expect(`.ic-mb-stub[data-reason=${reason}] h3`).toHaveText("Charts are not available on this panel");
            expect(`.ic-mb-stub[data-reason=${reason}]`).toHaveText(/managed by your provider/);
            expect(`.ic-mb-stub[data-reason=${reason}] button`).toHaveCount(0);
        }
    });

    test("a refused credential is told apart, with since when", async () => {
        const { asked } = await mountBoard({
            ok: false, reason: "credential", since: "2026-11-12T09:30:00+00:00",
            error: "The metrics central refused this panel's credential.",
        });
        const card = ".ic-mb-stub[data-reason=credential]";
        expect(card).toHaveText(/refused this panel's credential/);
        expect(`${card} .ic-mb-since`).toHaveText(/^Failing since /);
        expect(card).toHaveText(/Check Credential in Settings › Monitoring › Advanced/);
        expect(card).not.toHaveText(/could not be reached/);
        // It may come back on its own: still read every 45 s.
        await advanceTime(REFRESH_MS);
        expect(asked()).toBe(2);
    });

    test("a central down says since when it fails, when the panel knows", async () => {
        await mountBoard({
            ok: false, reason: "central", since: null,
            error: "The metrics central could not be reached.",
        });
        expect(".ic-mb-stub[data-reason=central]").toHaveText(/could not be reached/);
        expect(".ic-mb-since").toHaveCount(0);
    });

    test("a chart the central failed says so in its own card", async () => {
        await mountBoard({
            ok: true, start: 0, end: 3600, step: 30, timing: {},
            panels: {
                cpu: { series: [{ name: "odoo", role: "use", points: [[0, 1], [30, 2]] }] },
                pressure: { series: [], error: "The metrics central could not answer." },
                neighbours: { series: [], error: "The metrics central could not answer." },
            },
            diagnosis: { current: true, tone: "ok", findings: [], facts: {} },
            silence: { code: "window", since: null },
        });
        expect("[data-panel=pressure] .ic-mb-panel-error").toHaveText("The metrics central could not answer.");
        expect("[data-panel=pressure]").not.toHaveText(/No pressure readings/);
        expect("[data-panel=neighbours] .ic-mb-panel-error").toHaveCount(1);
        expect("[data-panel=cpu] .ic-mb-panel-error").toHaveCount(0);
        expect("[data-panel=cpu] canvas").toHaveCount(1);
    });

    test("an empty board keeps its first words and adds the cause", async () => {
        await mountBoard({
            ok: true, start: 0, end: 3600, step: 30, timing: {}, panels: {},
            diagnosis: { current: true, tone: "ok", findings: [], facts: {} },
            silence: { code: "new", since: null },
        });
        expect(".ic-mb-diag-text[data-silence=new]").toHaveText(
            /^No data for this period\. This instance has not reported yet/,
        );
    });
});

describe("OnDemandPanel, mounted", () => {
    beforeEach(() => allowTranslations());

    test("reads when pressed, and a second reading waits for the first", async () => {
        const asked = [];
        let release;
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                asked.push([url, params]);
                return new Promise((resolve) => {
                    release = () => resolve({ ok: true, rows: ["a", "b"] });
                });
            },
        });

        class Page extends Component {
            static template = xml`
                <div>
                    <OnDemandPanel title="'Queries now'" buttonLabel="'Read the database'"
                                   route="'/cloud/instance/db_activity'" params="{instance_id: 7}"
                                   busy="state.reading" onBusy.bind="setReading">
                        <t t-set-slot="default" t-slot-scope="scope">
                            <ul class="first"><li t-foreach="scope.data.rows" t-as="r" t-key="r" t-esc="r"/></ul>
                        </t>
                    </OnDemandPanel>
                    <OnDemandPanel title="'Slow routes'" buttonLabel="'Read the log'"
                                   route="'/cloud/instance/slow_routes'" params="{instance_id: 7}"
                                   busy="state.reading" onBusy.bind="setReading">
                        <t t-set-slot="default" t-slot-scope="scope"><p class="second">read</p></t>
                    </OnDemandPanel>
                </div>`;
            static components = { OnDemandPanel };
            static props = {};

            setup() {
                this.state = useState({ reading: false });
            }

            /** One reading at a time, as the board does. */
            setReading(value) {
                this.state.reading = value;
            }
        }

        await mountWithCleanup(Page, { env: { icon: relayIcon }, noMainContainer: true });
        await waitFor(".ic-od button");
        const buttons = () => document.querySelectorAll(".ic-od button");
        await click(buttons()[0]);
        await animationFrame();
        expect(asked).toEqual([["/cloud/instance/db_activity", { instance_id: 7 }]]);
        expect(buttons()[0]).toHaveText("Reading…");
        expect(buttons()[1]).toHaveAttribute("disabled");

        release();
        await animationFrame();
        await animationFrame();
        expect(".first li").toHaveCount(2);
        expect(buttons()[0]).toHaveText("Read again");
        expect(buttons()[1]).not.toHaveAttribute("disabled");
        expect(".ic-od-ago").toHaveCount(1);
    });

    test("a refused reading says why", async () => {
        patchWithCleanup(rpc, {
            _rpc() {
                return Promise.resolve({ ok: false, error: "This instance is not deployed." });
            },
        });

        class Page extends Component {
            static template = xml`
                <OnDemandPanel title="'Queries now'" buttonLabel="'Read the database'"
                               route="'/cloud/instance/db_activity'" params="{instance_id: 7}">
                    <t t-set-slot="default" t-slot-scope="scope"><p class="data">x</p></t>
                </OnDemandPanel>`;
            static components = { OnDemandPanel };
            static props = {};
        }

        await mountWithCleanup(Page, { env: { icon: relayIcon }, noMainContainer: true });
        await click(".ic-od button");
        await animationFrame();
        expect(".ic-od-error").toHaveText("This instance is not deployed.");
        expect(".data").toHaveCount(0);
    });
});
