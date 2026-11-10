import {Component, onMounted, onWillStart, useState} from "@odoo/owl";
import {loadBundle} from "@web/core/assets";
import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";
import {useSafePoll} from "../../utils/use_safe_poll";
import {chartLocale, MARKER_STYLES, TimeSeriesChart} from "./time_series_chart";
import {OnDemandPanel} from "./on_demand_panel";
import {formatBytes, formatDuration, formatNumber, formatPercent, formatSeconds} from "./formatters";

const {DateTime} = luxon;

/** Milliseconds between two refreshes of a short range shown unzoomed. */
export const REFRESH_MS = 45000;

/** Ranges that refresh on their own: a week does not move in 45 s. */
const LIVE_RANGES = new Set(["1h", "6h"]);

/** The windows the slow routes are read over, in minutes. */
export const SLOW_WINDOWS = [15, 60];

/**
 * The legend's words for the roles a panel draws by role.
 *
 * @returns {Object<string, string>}
 */
function roleLabels() {
  return {
    p50: _t("p50"),
    p95: _t("p95"),
    requests: _t("requests"),
    "5xx": _t("errors (5xx)"),
    cpu: _t("CPU"),
    memory: _t("memory"),
    io: _t("disk I/O"),
    directory: _t("directory"),
  };
}

/**
 * An instance's Metrics tab as a diagnosis: what is happening now and
 * what to look at, since when, which resource, which process or query,
 * and how big it is.
 *
 * One call to ``/cloud/instance/resource_series`` per refresh brings
 * every chart and the diagnosis, so a zoom on one chart moves them all.
 * The database's sessions and the slow routes are read from the host
 * only when asked (``OnDemandPanel``).
 */
export class MetricsBoard extends Component {
  static template = "incubacloud.MetricsBoard";
  static components = {TimeSeriesChart, OnDemandPanel};
  static props = {
    instanceId: {type: Number},
    canConfigure: {type: Boolean, optional: true},
    onShowRequests: {type: Function, optional: true},
  };
  static defaultProps = {canConfigure: true};

  setup() {
    this.ranges = [
      {key: "1h", label: _t("1 h")},
      {key: "6h", label: _t("6 h")},
      {key: "24h", label: _t("24 h")},
      {key: "7d", label: _t("7 d")},
    ];
    this.slowWindows = SLOW_WINDOWS;
    this.markerStyles = MARKER_STYLES;
    this.poll = useSafePoll();
    /** The refresh waiting to run (``setTimeout``'s handle), if any. */
    this.timer = null;
    this.state = useState({
      range: "1h",
      zoom: null,
      loading: true,
      error: "",
      reason: "",
      board: null,
      updatedAt: null,
      reading: false,
      slowMinutes: 15,
    });
    onWillStart(() => loadBundle("web.chartjs_lib"));
    onMounted(() => this.load());
  }

  /** Whether the charts show a zoomed window rather than a named range. */
  get isZoomed() {
    return !!this.state.zoom;
  }

  /**
   * Switch to a named range, dropping any zoom.
   *
   * @param {string} key one of ``this.ranges``
   */
  setRange(key) {
    this.state.range = key;
    this.state.zoom = null;
    this.load();
  }

  /**
   * Zoom every chart into a window one of them was dragged across.
   *
   * @param {number} start unix seconds
   * @param {number} end unix seconds
   */
  zoomTo(start, end) {
    this.state.zoom = {start, end};
    this.load();
  }

  /** Leave the zoomed window and go back to the selected range. */
  resetZoom() {
    this.state.zoom = null;
    this.load();
  }

  /**
   * Drop the refresh waiting to run, if any: a click that loads the
   * board starts the only chain of refreshes there is.
   */
  cancelRefresh() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /** Refresh in one period, in place of any refresh already waiting. */
  scheduleRefresh() {
    this.cancelRefresh();
    this.timer = this.poll.schedule(() => this.tick(), REFRESH_MS);
  }

  /**
   * Fetch the board and schedule the next refresh when the range is a
   * short one shown unzoomed.
   */
  async load() {
    this.cancelRefresh();
    const seq = (this.seq = (this.seq || 0) + 1);
    this.state.loading = true;
    const params = {instance_id: this.props.instanceId, range_key: this.state.range};
    if (this.state.zoom) {
      Object.assign(params, this.state.zoom);
    }
    let res;
    try {
      res = await rpc("/cloud/instance/resource_series", params);
    } catch (e) {
      res = {ok: false, error: e?.data?.message || e?.message || _t("Could not load the charts.")};
    }
    if (seq !== this.seq || !this.poll.alive) {
      return;
    }
    this.state.loading = false;
    if (res.ok) {
      this.state.error = "";
      this.state.reason = "";
      this.state.board = res;
      this.state.updatedAt = DateTime.now();
      console.debug("[metrics-board]", this.state.range, res.timing);
    } else {
      this.state.error = res.error || _t("Could not load the charts.");
      this.state.reason = res.reason || "";
      this.state.board = null;
    }
    if (LIVE_RANGES.has(this.state.range) && !this.state.zoom && this.state.reason !== "disabled") {
      this.scheduleRefresh();
    }
  }

  /** Refresh, or wait another period without asking while the tab is hidden. */
  tick() {
    if (document.hidden) {
      this.scheduleRefresh();
      return;
    }
    return this.load();
  }

  /** The window the charts span, from the last answer. */
  get window() {
    const b = this.state.board;
    return b ? {start: b.start, end: b.end} : {start: 0, end: 0};
  }

  /**
   * The answer for one panel.
   *
   * @param {string} key a panel's key
   * @returns {Object}
   */
  panel(key) {
    return this.state.board?.panels?.[key] || {series: [], markers: []};
  }

  /**
   * One panel's series, labelled for its legend.
   *
   * @param {string} key a panel's key
   * @returns {Array<Object>}
   */
  series(key) {
    const labels = roleLabels();
    return (this.panel(key).series || []).map((s) => ({...s, label: labels[s.role] || s.role}));
  }

  /** The moments (restarts, out-of-memory kills) of the window. */
  get markers() {
    return this.panel("events").markers || [];
  }

  /** Whether any panel has anything to draw. */
  get hasData() {
    const panels = this.state.board?.panels || {};
    return Object.values(panels).some((p) => (p.series || []).some((s) => s.points.length));
  }

  /**
   * Whether one panel has a line to draw.
   *
   * @param {string} key a panel's key
   * @returns {boolean}
   */
  hasSeries(key) {
    return (this.panel(key).series || []).some((s) => s.points.length);
  }

  /** This instance's id as the neighbours' series carry it. */
  get instanceKey() {
    return String(this.props.instanceId);
  }

  /** The diagnosis of the last answer, or an empty one. */
  get diagnosis() {
    return this.state.board?.diagnosis || {current: false, tone: "ok", findings: [], facts: {}};
  }

  /** The heading of the diagnosis card, by its tone. */
  get diagnosisHeading() {
    const tone = this.diagnosis.tone;
    if (tone === "bad" || tone === "warn") {
      return _t("Something is short");
    }
    return _t("Nothing looks saturated");
  }

  /** Workers line: configured and estimated occupancy. */
  get workersText() {
    const configured = this.state.board?.workers?.configured || 0;
    if (!configured) {
      return _t("Odoo runs without workers (threaded mode).");
    }
    const occupancy = this.diagnosis.facts?.occupancy;
    if (occupancy === null || occupancy === undefined) {
      return _t("%s workers configured.", configured);
    }
    return _t("%(n)s workers configured · estimated occupancy %(pct)s (Odoo's CPU over the last 15 minutes against one core per worker).", {
      n: configured,
      pct: formatPercent(occupancy),
    });
  }

  /** The last size of the instance's directory, if known. */
  get directorySize() {
    const s = (this.panel("disk").series || [])[0];
    return s?.points.length ? formatBytes(s.points[s.points.length - 1][1]) : "—";
  }

  /** "Updated at …", in the reader's language and time zone. */
  get updatedText() {
    const at = this.state.updatedAt;
    return at ? at.setLocale(chartLocale() || "en").toLocaleString(DateTime.TIME_WITH_SECONDS) : "";
  }

  /**
   * Bring a section of the board into view.
   *
   * @param {string} id the section's element id
   */
  scrollTo(id) {
    document.getElementById(id)?.scrollIntoView({behavior: "smooth", block: "start"});
  }

  /**
   * One reading of a board runs at a time.
   *
   * @param {boolean} value
   */
  setReading(value) {
    this.state.reading = value;
  }

  /**
   * Choose the window of the slow routes.
   *
   * @param {Event} ev the select's change
   */
  onSlowWindow(ev) {
    this.state.slowMinutes = Number(ev.target.value) || 15;
  }

  /**
   * Seconds for the template, in ms below one.
   *
   * @param {number} v
   * @returns {string}
   */
  fmtSeconds(v) {
    return formatSeconds(v);
  }

  /**
   * An age for the template ("6 min 12 s").
   *
   * @param {number} v seconds
   * @returns {string}
   */
  fmtDuration(v) {
    return formatDuration(v);
  }

  /**
   * A percentage for the template; a dash when there is none.
   *
   * @param {number|null} v
   * @returns {string}
   */
  fmtPercent(v) {
    return v === null || v === undefined ? "—" : formatPercent(v);
  }

  /**
   * A number for the template, in the reader's language.
   *
   * @param {number} v
   * @param {number} digits fraction digits, at most
   * @returns {string}
   */
  fmtNumber(v, digits = 0) {
    return formatNumber(v, digits);
  }

  /**
   * A moment of the log (ISO, UTC) in the reader's language and zone.
   *
   * @param {string} iso
   * @returns {string}
   */
  fmtAt(iso) {
    if (!iso) {
      return "—";
    }
    return DateTime.fromISO(iso).setLocale(chartLocale() || "en").toLocaleString(DateTime.DATETIME_SHORT);
  }

  /**
   * The line over the slow routes: how many requests, and the workers
   * recycled for memory.
   *
   * @param {Object} data the reading
   * @returns {string}
   */
  slowSummary(data) {
    return _t(
      "%(n)s requests in the last %(minutes)s minutes · workers recycled for memory: %(recycled)s (%(rate)s an hour)",
      {
        n: data.requests,
        minutes: data.minutes,
        recycled: data.recycles.count,
        rate: formatNumber(data.recycles.per_hour, 1),
      },
    );
  }

  /**
   * A scheduled action still running, since when.
   *
   * @param {string} iso when it started (UTC)
   * @returns {string}
   */
  runningSince(iso) {
    return _t("running since %s", this.fmtAt(iso));
  }

  /** "Updated at …, step …", under the board. */
  get metaText() {
    const live = LIVE_RANGES.has(this.state.range) && !this.isZoomed;
    const args = {at: this.updatedText, step: this.state.board?.step || 0};
    return live
      ? _t("Updated %(at)s · step %(step)s s · refreshes every 45 s", args)
      : _t("Updated %(at)s · step %(step)s s", args);
  }

  /**
   * The "Queries now" line, in words.
   *
   * @param {Object} s the reading's ``summary``
   * @returns {string}
   */
  dbSummary(s) {
    return _t(
      "%(conn)s/%(max)s connections · %(active)s active · %(waiting)s waiting on a lock · longest %(longest)s · idle in a transaction %(idle)s · cache hits %(hit)s · size %(size)s",
      {
        conn: s.connections,
        max: s.max_connections,
        active: s.active,
        waiting: s.waiting_lock,
        longest: formatDuration(s.longest_active),
        idle: s.idle_in_transaction,
        hit: s.cache_hit_ratio === null ? "—" : formatPercent(s.cache_hit_ratio),
        size: formatBytes(s.size_bytes),
      },
    );
  }

  /**
   * The wait of one session, in a few words.
   *
   * @param {Object} row a session
   * @returns {string}
   */
  waitText(row) {
    if (!row.wait_event_type) {
      return "—";
    }
    return row.wait_event ? `${row.wait_event_type} · ${row.wait_event}` : row.wait_event_type;
  }

  /**
   * Who a session blocks and who blocks it.
   *
   * @param {Object} row a session
   * @returns {string}
   */
  blockText(row) {
    const parts = [];
    if (row.blocked_by?.length) {
      parts.push(_t("waits for %s", row.blocked_by.join(", ")));
    }
    if (row.blocks?.length) {
      parts.push(_t("blocks %s", row.blocks.join(", ")));
    }
    return parts.join(" · ");
  }
}
