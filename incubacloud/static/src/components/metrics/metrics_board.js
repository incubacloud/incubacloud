import {_t} from "@web/core/l10n/translation";
import {ChartBoard} from "./chart_board";
import {MARKER_STYLES, TimeSeriesChart} from "./time_series_chart";
import {OnDemandPanel} from "./on_demand_panel";
import {formatBytes, formatDuration, formatNumber, formatPercent} from "./formatters";

export {REFRESH_MS} from "./chart_board";

/** The windows the slow routes are read over, in minutes. */
export const SLOW_WINDOWS = [15, 60];

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
export class MetricsBoard extends ChartBoard {
  static template = "incubacloud.MetricsBoard";
  static components = {TimeSeriesChart, OnDemandPanel};
  static props = {
    instanceId: {type: Number},
    canConfigure: {type: Boolean, optional: true},
    onShowRequests: {type: Function, optional: true},
  };
  static defaultProps = {canConfigure: true};

  setup() {
    super.setup();
    this.slowWindows = SLOW_WINDOWS;
    this.markerStyles = MARKER_STYLES;
  }

  /**
   * The readings on demand: one at a time, and the slow routes' window.
   *
   * @returns {Object}
   */
  extraState() {
    return {reading: false, slowMinutes: 15};
  }

  /** The route that answers the whole board. */
  get route() {
    return "/cloud/instance/resource_series";
  }

  /**
   * The instance the board is about.
   *
   * @returns {Object}
   */
  routeParams() {
    return {instance_id: this.props.instanceId};
  }

  /** The moments (restarts, out-of-memory kills) of the window. */
  get markers() {
    return this.panel("events").markers || [];
  }

  /**
   * Why the board has no line to draw, in words, from the server's
   * ``silence``: the server's agents, then the instance itself.
   */
  get silenceText() {
    const silence = this.state.board?.silence || {};
    switch (silence.code) {
      case "agents_missing":
        return _t("The metrics agents are not installed on this instance's server yet. The panel installs them on its own.");
      case "agents_failed":
        return _t("The metrics agents could not be installed on this instance's server. The panel keeps trying on its own.");
      case "agents_waiting":
        return _t("The agents on this instance's server are installed but have not sent anything yet. The first readings arrive within a few minutes.");
      case "host_silent":
        return _t("This instance's server stopped sending metrics on %s. Check that the server is on and reachable.", this.fmtAt(silence.since));
      case "stopped":
        return _t("This instance is stopped. Start it to see its charts again.");
      case "new":
        return _t("This instance has not reported yet: a new instance shows data once its containers run.");
      default:
        return _t("Nothing was reported in the selected window. Pick a longer range or reset the zoom.");
    }
  }

  /** Where to look next about the server, for whoever manages hosts. */
  get silenceHint() {
    const code = this.state.board?.silence?.code;
    const aboutServer = ["agents_missing", "agents_failed", "agents_waiting", "host_silent"].includes(code);
    if (!aboutServer || !this.env.permissions?.can_manage_hosts) {
      return "";
    }
    return _t("The Monitoring line on the server's page, under Hosts, shows how it is going.");
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
