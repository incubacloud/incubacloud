import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, waitFor } from "@odoo/hoot-dom";
import { advanceTime, animationFrame } from "@odoo/hoot-mock";
import { Component, useState, xml } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
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
