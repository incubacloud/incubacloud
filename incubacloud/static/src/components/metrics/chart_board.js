import {Component, onMounted, onWillStart, useState} from "@odoo/owl";
import {loadBundle} from "@web/core/assets";
import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";
import {useSafePoll} from "../../utils/use_safe_poll";
import {chartLocale} from "./time_series_chart";
import {formatBytes, formatDuration, formatNumber, formatPercent, formatSeconds, formatValue} from "./formatters";

const {DateTime} = luxon;

/** Milliseconds between two refreshes of a short range shown unzoomed. */
export const REFRESH_MS = 45000;

/** Ranges that refresh on their own: a week does not move in 45 s. */
export const LIVE_RANGES = new Set(["1h", "6h"]);

/**
 * Answers that a refresh would only repeat: observability off, or on
 * with no central. Settings change them, and opening the page again
 * reads them again.
 */
export const SETTLED_REASONS = new Set(["disabled", "unconfigured"]);

/**
 * The legend's words for the roles a panel draws by role.
 *
 * @returns {Object<string, string>}
 */
export function roleLabels() {
  return {
    p50: _t("p50"),
    p95: _t("p95"),
    requests: _t("requests"),
    "5xx": _t("errors (5xx)"),
    cpu: _t("CPU"),
    memory: _t("memory"),
    io: _t("disk I/O"),
    directory: _t("directory"),
    load1: _t("last minute"),
    load15: _t("last 15 minutes"),
    used: _t("in use"),
    swap: _t("swap"),
    read: _t("read"),
    write: _t("written"),
    received: _t("received"),
    sent: _t("sent"),
  };
}

/**
 * What every board of the panel's own charts shares: a range and a zoom,
 * one read of a route per refresh that brings every chart, a refresh
 * every 45 s on a short range shown unzoomed (never while the tab is
 * hidden, never two chains at once), and the words for an answer that
 * has nothing to draw.
 *
 * A board names its route (``route``) and what it sends besides the
 * window (``routeParams()``); the instance's, the server's and the
 * fleet's boards extend it.
 */
export class ChartBoard extends Component {
  static template = "";
  static props = {};

  setup() {
    this.ranges = [
      {key: "1h", label: _t("1 h")},
      {key: "6h", label: _t("6 h")},
      {key: "24h", label: _t("24 h")},
      {key: "7d", label: _t("7 d")},
    ];
    this.poll = useSafePoll();
    /** The refresh waiting to run (``setTimeout``'s handle), if any. */
    this.timer = null;
    this.state = useState({
      range: "1h",
      zoom: null,
      loading: true,
      error: "",
      reason: "",
      since: null,
      board: null,
      updatedAt: null,
      ...this.extraState(),
    });
    onWillStart(() => loadBundle("web.chartjs_lib"));
    onMounted(() => this.load());
  }

  /**
   * State a board keeps besides the window and the last answer.
   *
   * @returns {Object}
   */
  extraState() {
    return {};
  }

  /** The route that answers the whole board. */
  get route() {
    return "";
  }

  /**
   * What the route is sent besides the range and the zoom.
   *
   * @returns {Object}
   */
  routeParams() {
    return {};
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
    const params = {...this.routeParams(), range_key: this.state.range};
    if (this.state.zoom) {
      Object.assign(params, this.state.zoom);
    }
    let res;
    try {
      res = await rpc(this.route, params);
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
      this.state.since = null;
      this.state.board = res;
      this.state.updatedAt = DateTime.now();
      console.debug("[metrics-board]", this.route, this.state.range, res.timing);
    } else if (res.reason === "busy" && this.state.board) {
      // A central over its cap answers again in a moment: the last
      // board stays, with a quiet line saying so (``ChartBoardBusy``).
      this.state.error = "";
      this.state.reason = "busy";
      this.state.since = null;
    } else {
      this.state.error = res.error || _t("Could not load the charts.");
      this.state.reason = res.reason || "";
      this.state.since = res.since || null;
      this.state.board = null;
    }
    if (LIVE_RANGES.has(this.state.range) && !this.state.zoom && !SETTLED_REASONS.has(this.state.reason)) {
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
   * One panel's series, labelled for its legend; the line that adds up
   * what a stacked panel left out is named "others".
   *
   * @param {string} key a panel's key
   * @returns {Array<Object>}
   */
  series(key) {
    const labels = roleLabels();
    return (this.panel(key).series || []).map((s) => ({
      ...s,
      name: s.role === "others" ? _t("others") : s.name,
      label: labels[s.role] || s.role,
    }));
  }

  /**
   * Why one panel could not be read, when the central failed it alone.
   *
   * @param {string} key a panel's key
   * @returns {string} the server's words, or "" when it was answered
   */
  panelError(key) {
    return this.panel(key).error || "";
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

  /** Whether this person can open Settings › Monitoring from here. */
  get canOpenSettings() {
    return !!(this.props.canConfigure && this.env.permissions?.can_manage_settings);
  }

  /** Open Settings › Monitoring, where observability is set up. */
  openSettings() {
    this.env.navigate("settings", {section: "monitoring"});
  }

  /** "Failing since …" under a central that fails, when the panel knows. */
  get sinceText() {
    return this.state.since ? _t("Failing since %s.", this.fmtAt(this.state.since)) : "";
  }

  /** What to do about a credential the central refuses. */
  get credentialHint() {
    if (!this.props.canConfigure) {
      return _t("Metrics are managed by your provider. Let them know if this persists.");
    }
    return _t("Check Credential in Settings › Monitoring › Advanced: it must be the one the central was deployed with.");
  }

  /** "Updated at …", in the reader's language and time zone. */
  get updatedText() {
    const at = this.state.updatedAt;
    return at ? at.setLocale(chartLocale() || "en").toLocaleString(DateTime.TIME_WITH_SECONDS) : "";
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
   * A value in a panel's unit, for the template.
   *
   * @param {string|null} unit
   * @param {number|null} v
   * @returns {string}
   */
  fmtValue(unit, v) {
    return v === null || v === undefined ? "—" : formatValue(unit, v);
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
   * Bytes for the template; a dash when there are none.
   *
   * @param {number|null} v
   * @returns {string}
   */
  fmtBytes(v) {
    return v === null || v === undefined ? "—" : formatBytes(v);
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
   * A moment (ISO, UTC) in the reader's language and zone.
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
}
