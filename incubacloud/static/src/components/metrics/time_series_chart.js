import {Component, onMounted, onPatched, onWillUnmount, useRef} from "@odoo/owl";
import {_t} from "@web/core/l10n/translation";
import {user} from "@web/core/user";
import {formatValue} from "./formatters";

/** Token each known compose service is drawn with; others take a tag colour. */
export const SERVICE_TOKENS = {odoo: "--rl-green", db: "--rl-cyan", backup: "--rl-amber"};

/** Token of each role a panel draws by role rather than by name. */
export const ROLE_TOKENS = {
  p50: "--rl-cyan",
  p95: "--rl-amber",
  requests: "--rl-green",
  "5xx": "--rl-red",
  cpu: "--rl-green",
  memory: "--rl-amber",
  io: "--rl-cyan",
  directory: "--rl-green",
};

/** Colours handed out, in order, to lines with no colour of their own. */
export const SPARE_TOKENS = [
  "--rl-tag-6", "--rl-tag-1", "--rl-tag-2", "--rl-tag-8", "--rl-tag-9",
  "--rl-tag-3", "--rl-tag-4", "--rl-tag-5", "--rl-tag-7", "--rl-tag-0",
];

/** Roles drawn as a dashed ceiling over the line of the same name. */
const LIMIT_ROLES = new Set(["limit"]);

/** Token and glyph of each kind of moment drawn on the time axis. */
export const MARKER_STYLES = {
  restart: {token: "--rl-cyan", glyph: "●"},
  oom: {token: "--rl-red", glyph: "▲"},
};

/** Pixels a pointer must travel before a drag counts as a zoom. */
const DRAG_PX = {mouse: 8, pen: 8, touch: 12};

/**
 * Read a CSS custom property from <html>, where the panel's theme lives.
 *
 * @param {string} name e.g. ``--rl-green``
 * @returns {string} the trimmed value, "" when unset
 */
export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * Turn ``[[ts, value], ...]`` into Chart.js points, with a null wherever
 * the backend skipped more than two steps, so a gap reads as a gap and
 * not as a straight line across it.
 *
 * @param {Array<Array<number>>} points unix seconds and value
 * @param {number} step seconds between points
 * @returns {Array<{x: number, y: number|null}>}
 */
export function toChartPoints(points, step) {
  const out = [];
  let prev = null;
  for (const [ts, value] of points) {
    if (prev !== null && ts - prev > 2 * step) {
      out.push({x: (prev + step) * 1000, y: null});
    }
    out.push({x: ts * 1000, y: value});
    prev = ts;
  }
  return out;
}

/**
 * The luxon locale of the reader: Odoo's language tag with a hyphen.
 *
 * @returns {string|undefined}
 */
export function chartLocale() {
  return user.lang ? user.lang.replace("_", "-") : undefined;
}

/**
 * Time-axis unit that keeps tick labels readable for a span.
 *
 * @param {number} spanMs window length in milliseconds
 * @returns {"minute"|"hour"|"day"}
 */
export function timeUnit(spanMs) {
  const hours = spanMs / 3600000;
  return hours <= 3 ? "minute" : hours <= 72 ? "hour" : "day";
}

/**
 * Chart.js plugin: the moments (restarts, out-of-memory kills) as
 * vertical lines with a glyph, a vertical line under the hovered
 * instant, and the drag-to-zoom band. Drawn by the chart itself so they
 * follow its layout on resize.
 */
const overlayPlugin = {
  id: "icOverlay",
  /**
   * Draw markers, crosshair and the selection band after the datasets.
   *
   * @param {Object} chart the Chart.js instance
   */
  afterDatasetsDraw(chart) {
    const {ctx, chartArea, scales} = chart;
    ctx.save();
    for (const marker of chart.options.icMarkers || []) {
      const x = scales.x.getPixelForValue(marker.ts * 1000);
      if (x < chartArea.left || x > chartArea.right) {
        continue;
      }
      const style = MARKER_STYLES[marker.role] || MARKER_STYLES.restart;
      const color = token(style.token) || "#888";
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top + 8);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "10px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(style.glyph, x, chartArea.top + 7);
    }
    const active = chart.tooltip?.getActiveElements?.() || [];
    if (active.length) {
      const x = active[0].element.x;
      ctx.strokeStyle = chart.options.icCrosshair || "rgba(128,128,128,.5)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
    }
    const drag = chart.$icDrag;
    if (drag && Math.abs(drag.x1 - drag.x0) > 2) {
      ctx.setLineDash([]);
      ctx.fillStyle = chart.options.icBand || "rgba(128,128,128,.15)";
      const left = Math.max(chartArea.left, Math.min(drag.x0, drag.x1));
      const right = Math.min(chartArea.right, Math.max(drag.x0, drag.x1));
      ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
    }
    ctx.restore();
  },
};

/**
 * One time series chart, drawn with the Chart.js Odoo ships
 * (``web.chartjs_lib``; the board loads it before mounting this).
 *
 * Lines are coloured by compose service (``palette="service"``), by role
 * (``"role"``: p50/p95, requests/5xx, cpu/memory/io) or, for the
 * instances of one server (``"neighbours"``), with the one the page is
 * about in green and thicker. A ``limit`` role is the dashed ceiling of
 * the line of the same name. Dates follow the reader's language and time
 * zone; values the panel's unit. Dragging across it (mouse, pen or a
 * finger moving sideways) asks ``onZoom`` for that window.
 */
export class TimeSeriesChart extends Component {
  static template = "incubacloud.TimeSeriesChart";
  static props = {
    series: {type: Array},
    markers: {type: Array, optional: true},
    window: {type: Object},
    step: {type: Number},
    unit: {type: String, optional: true},
    palette: {type: String, optional: true},
    highlightId: {type: String, optional: true},
    stacked: {type: Boolean, optional: true},
    label: {type: String},
    onZoom: {type: Function, optional: true},
  };
  static defaultProps = {markers: [], palette: "service", highlightId: "", stacked: false};

  setup() {
    this.canvasRef = useRef("canvas");
    this.chart = null;
    onMounted(() => {
      this.draw();
      this.themeObserver = new MutationObserver(() => this.repaint());
      this.themeObserver.observe(document.documentElement, {
        attributes: true, attributeFilter: ["data-ic-theme"],
      });
    });
    onPatched(() => this.draw());
    onWillUnmount(() => {
      this.themeObserver?.disconnect();
      this.chart?.destroy();
      this.chart = null;
    });
  }

  /**
   * Colour token of one line.
   *
   * @param {Object} series ``{name, role, id}``
   * @param {Object} spare counter of the spare colours handed out
   * @returns {string}
   */
  lineToken(series, spare) {
    const {palette, highlightId} = this.props;
    if (palette === "role") {
      return ROLE_TOKENS[series.role] || SPARE_TOKENS[spare.n++ % SPARE_TOKENS.length];
    }
    if (palette === "neighbours") {
      return series.id === highlightId
        ? "--rl-green"
        : SPARE_TOKENS[spare.n++ % SPARE_TOKENS.length];
    }
    return SERVICE_TOKENS[series.name] || (spare.byName[series.name] ??= SPARE_TOKENS[spare.n++ % SPARE_TOKENS.length]);
  }

  /** Chart.js datasets for the current props. */
  datasets() {
    const spare = {n: 0, byName: {}};
    const {palette, highlightId, step, stacked} = this.props;
    return this.props.series.map((s) => {
      const limit = LIMIT_ROLES.has(s.role);
      const mine = palette === "neighbours" && s.id === highlightId;
      const name = palette === "role" ? (s.label || s.role) : s.name;
      return {
        label: limit ? `${s.name} · ${this.limitLabel}` : name,
        data: toChartPoints(s.points, step),
        icToken: this.lineToken(s, spare),
        borderWidth: mine ? 2.6 : limit ? 1.2 : 1.6,
        borderDash: limit ? [6, 4] : [],
        pointRadius: 0,
        pointHoverRadius: limit ? 0 : 3,
        tension: 0.25,
        spanGaps: false,
        fill: stacked && !limit ? "stack" : false,
        stack: stacked && !limit ? "use" : undefined,
      };
    });
  }

  /** The legend's word for a dashed ceiling. */
  get limitLabel() {
    return _t("limit");
  }

  /** Build the chart, or replace its data and window when it exists. */
  draw() {
    const canvas = this.canvasRef.el;
    if (!canvas || !window.Chart) {
      return;
    }
    const {start, end} = this.props.window;
    const unit = this.props.unit;
    const datasets = this.datasets();
    const span = (end - start) * 1000;
    const fmt = (v) => formatValue(unit, v);
    if (this.chart && this.chart.canvas === canvas) {
      const {x, y} = this.chart.options.scales;
      this.chart.data.datasets = datasets;
      x.min = start * 1000;
      x.max = end * 1000;
      x.time.unit = timeUnit(span);
      y.ticks.callback = fmt;
      y.stacked = this.props.stacked;
      this.chart.options.icMarkers = this.props.markers;
      this.paint();
      this.chart.update("none");
      return;
    }
    this.chart?.destroy();
    this.chart = new window.Chart(canvas, {
      type: "line",
      data: {datasets},
      plugins: [overlayPlugin],
      options: {
        animation: false,
        maintainAspectRatio: false,
        responsive: true,
        normalized: true,
        parsing: false,
        interaction: {mode: "index", intersect: false},
        icMarkers: this.props.markers,
        scales: {
          x: {
            type: "time",
            min: start * 1000,
            max: end * 1000,
            adapters: {date: {locale: chartLocale(), zone: user.tz || undefined}},
            time: {
              unit: timeUnit(span),
              tooltipFormat: "ccc d LLL, t",
              displayFormats: {minute: "t", hour: "t", day: "ccc d"},
            },
            ticks: {maxRotation: 0, autoSkipPadding: 24, maxTicksLimit: 8},
            grid: {drawTicks: false},
            border: {display: false},
          },
          y: {
            beginAtZero: true,
            stacked: this.props.stacked,
            ticks: {callback: fmt, maxTicksLimit: 5, padding: 8},
            border: {display: false},
          },
        },
        plugins: {
          legend: {
            position: "bottom",
            align: "start",
            labels: {usePointStyle: true, pointStyle: "line", boxWidth: 18, padding: 14},
          },
          tooltip: {
            padding: 10,
            boxPadding: 4,
            usePointStyle: true,
            callbacks: {
              label: (ctx) => ` ${ctx.dataset.label}: ${formatValue(this.props.unit, ctx.parsed.y)}`,
            },
          },
        },
      },
    });
    this.paint();
    this.chart.update("none");
    this.bindZoom(this.chart);
  }

  /** Colour the chart from the panel's current theme tokens. */
  paint() {
    const chart = this.chart;
    const fg = token("--rl-fg");
    const muted = token("--rl-muted");
    const line = token("--rl-line");
    const mono = token("--rl-fm") || "monospace";
    const body = token("--rl-fb") || "sans-serif";
    for (const ds of chart.data.datasets) {
      const color = token(ds.icToken) || muted;
      ds.borderColor = color;
      ds.backgroundColor = ds.fill ? `color-mix(in srgb, ${color} 22%, transparent)` : color;
    }
    const {x, y} = chart.options.scales;
    for (const axis of [x, y]) {
      axis.ticks.color = muted;
      axis.ticks.font = {family: mono, size: 11};
      axis.grid.color = line;
    }
    x.grid.display = false;
    const {legend, tooltip} = chart.options.plugins;
    legend.labels.color = token("--rl-fg2") || fg;
    legend.labels.font = {family: body, size: 12};
    tooltip.backgroundColor = token("--rl-surf2");
    tooltip.borderColor = token("--rl-line2");
    tooltip.borderWidth = 1;
    tooltip.titleColor = fg;
    tooltip.bodyColor = token("--rl-fg2") || fg;
    tooltip.titleFont = {family: body, size: 12, weight: "600"};
    tooltip.bodyFont = {family: mono, size: 11.5};
    chart.options.icCrosshair = token("--rl-line2");
    chart.options.icBand = `rgba(${token("--rl-green-rgb") || "128,128,128"}, .14)`;
  }

  /** Repaint after the panel's theme changed. */
  repaint() {
    if (this.chart) {
      this.paint();
      this.chart.update("none");
    }
  }

  /**
   * Drag across the chart to zoom into that window. A finger zooms only
   * when it moves sideways: the canvas keeps ``touch-action: pan-y``, so
   * scrolling the page up and down still works over it.
   *
   * @param {Object} chart the Chart.js instance
   */
  bindZoom(chart) {
    const canvas = chart.canvas;
    const inside = (x) => x >= chart.chartArea.left && x <= chart.chartArea.right;
    canvas.addEventListener("pointerdown", (ev) => {
      if (!this.props.onZoom || (ev.pointerType === "mouse" && ev.button !== 0) || !inside(ev.offsetX)) {
        return;
      }
      chart.$icDrag = {x0: ev.offsetX, x1: ev.offsetX, type: ev.pointerType};
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (chart.$icDrag) {
        chart.$icDrag.x1 = ev.offsetX;
        chart.draw();
      }
    });
    const finish = (ev) => {
      const drag = chart.$icDrag;
      chart.$icDrag = null;
      if (!drag) {
        return;
      }
      drag.x1 = ev.offsetX;
      if (Math.abs(drag.x1 - drag.x0) < (DRAG_PX[drag.type] || 8)) {
        chart.draw();
        return;
      }
      const scale = chart.scales.x;
      const a = scale.getValueForPixel(Math.min(drag.x0, drag.x1));
      const b = scale.getValueForPixel(Math.max(drag.x0, drag.x1));
      this.props.onZoom(Math.floor(a / 1000), Math.ceil(b / 1000));
    };
    canvas.addEventListener("pointerup", finish);
    canvas.addEventListener("pointercancel", () => {
      chart.$icDrag = null;
      chart.draw();
    });
    canvas.addEventListener("pointerleave", (ev) => chart.$icDrag && finish(ev));
  }
}
