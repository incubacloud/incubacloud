import {_t} from "@web/core/l10n/translation";
import {ChartBoard} from "./chart_board";
import {TimeSeriesChart} from "./time_series_chart";
import {formatDuration} from "./formatters";

/**
 * A server's performance as the panel draws it: what is happening now,
 * its load and memory, which instance uses it, its disks, its network
 * and requests, and how fresh the readings are.
 *
 * One call to ``/cloud/host/resource_series`` per refresh brings every
 * chart, the "who uses it" table and the diagnosis, so a zoom on one
 * chart moves them all. Shown in the server's Metrics tab and in
 * Monitoring's Hosts view.
 */
export class HostBoard extends ChartBoard {
  static template = "incubacloud.HostBoard";
  static components = {TimeSeriesChart};
  static props = {
    hostId: {type: Number},
    canConfigure: {type: Boolean, optional: true},
  };
  static defaultProps = {canConfigure: true};

  /** The route that answers the whole board. */
  get route() {
    return "/cloud/host/resource_series";
  }

  /**
   * The server the board is about.
   *
   * @returns {Object}
   */
  routeParams() {
    return {host_id: this.props.hostId};
  }

  /**
   * The chart cards of one section of the board, in order, their words
   * translated here so the card template stays generic.
   *
   * @param {string} section ``load``, ``use``, ``disk`` or ``traffic``
   * @returns {Array<Object>}
   */
  cardsOf(section) {
    const noInstance = _t("No instance of this server has reported in this window.");
    const cards = {
      load: [
        {key: "load", title: _t("Load per core"), hint: _t("tasks waiting or running, per core · above 1 they queue"),
          palette: "role", label: _t("Load per core of this server over time"),
          empty: _t("No load readings from this server in this window.")},
        {key: "pressure", title: _t("Waiting for resources"), hint: _t("% of the time some task waited"),
          unit: "percent", palette: "role", label: _t("Time this server's tasks waited for CPU, memory or disk"),
          empty: _t("No pressure readings from this server. Its kernel may not report them.")},
        {key: "memory", title: _t("Memory in use"), hint: _t("% of the server's memory · and of its swap"),
          unit: "percent", palette: "role", label: _t("Share of this server's memory and swap in use over time"),
          empty: _t("No memory readings from this server in this window."), wide: true},
      ],
      use: [
        {key: "cpu_instances", title: _t("CPU per instance"), hint: _t("stacked · 100 % = one core"),
          unit: "percent", stacked: true, label: _t("CPU use of every instance on this server over time"), empty: noInstance},
        {key: "memory_instances", title: _t("Memory per instance"), hint: _t("stacked · working set"),
          unit: "bytes", stacked: true, label: _t("Memory use of every instance on this server over time"), empty: noInstance},
      ],
      disk: [
        {key: "root", title: _t("Root disk in use"), hint: _t("% of the root file system"),
          unit: "percent", palette: "role", label: _t("Share of this server's root disk in use over time"),
          empty: _t("No disk readings from this server in this window.")},
        {key: "io", title: _t("Disk busy"), hint: _t("% of the time each disk had work"),
          unit: "percent", label: _t("Share of the time each disk of this server was busy"),
          empty: _t("No disk activity readings from this server in this window.")},
        {key: "io_bytes", title: _t("Disk throughput"), hint: _t("read and written per second"),
          unit: "bytes_rate", palette: "role", label: _t("Bytes this server's disks read and wrote per second"),
          empty: _t("No disk activity readings from this server in this window.")},
        {key: "disk_instances", title: _t("Directory per instance"), hint: _t("stacked · measured every 10 minutes"),
          unit: "bytes", stacked: true, label: _t("Size of every instance's directory on this server over time"),
          empty: _t("The server measures the directories every 10 minutes; nothing has been reported in this window.")},
      ],
      traffic: [
        {key: "network", title: _t("Network"), hint: _t("received and sent per second"),
          unit: "bytes_rate", palette: "role", label: _t("Bytes this server received and sent per second"),
          empty: _t("No network readings from this server in this window.")},
        {key: "requests", title: _t("Requests per instance"), hint: _t("stacked · per second, at the proxy"),
          unit: "rps", stacked: true, label: _t("Requests per second each instance on this server answered"),
          empty: _t("No instance of this server answered requests in this window.")},
        {key: "latency", title: _t("Request time per instance"), hint: _t("p95, at the proxy"),
          unit: "seconds", label: _t("p95 of the request time of every instance on this server"),
          empty: _t("No instance of this server answered requests in this window."), wide: true},
      ],
    };
    return cards[section] || [];
  }

  /** The diagnosis of the last answer, or an empty one. */
  get diagnosis() {
    return this.state.board?.diagnosis || {current: false, tone: "ok", findings: []};
  }

  /** The heading of the diagnosis card, by its tone. */
  get diagnosisHeading() {
    const tone = this.diagnosis.tone;
    return tone === "bad" || tone === "warn" ? _t("Something is short") : _t("Normal");
  }

  /** The server's own figures: cores, memory, root disk, ingestion age. */
  get facts() {
    return this.panel("host").value || {};
  }

  /** "4 cores · 7.8 GiB of memory · 80 GiB root disk", when known. */
  get factsText() {
    const f = this.facts;
    const parts = [];
    if (f.cores) {
      parts.push(_t("%s cores", f.cores));
    }
    if (f.memory_total) {
      parts.push(_t("%s of memory", this.fmtBytes(f.memory_total)));
    }
    if (f.root_size) {
      parts.push(_t("%s root disk", this.fmtBytes(f.root_size)));
    }
    return parts.join(" · ");
  }

  /** How long ago the server's agents sent their last sample. */
  get ingestText() {
    const age = this.facts.ingest_age;
    if (age === null || age === undefined) {
      return _t("No sample from this server in the last hour.");
    }
    return _t("Last sample from this server %s ago.", formatDuration(age));
  }

  /** Whether the last sample is older than the alert's five minutes. */
  get ingestLate() {
    const age = this.facts.ingest_age;
    return age === null || age === undefined || age > 300;
  }

  /** The rows of "who uses it", busiest first. */
  get consumers() {
    return this.panel("consumers").rows || [];
  }

  /**
   * Whether a row can open its instance's page.
   *
   * @param {Object} row
   * @returns {boolean}
   */
  canOpen(row) {
    return !!row.project_id;
  }

  /**
   * Open an instance of the table.
   *
   * @param {Object} row a row with ``project_id`` and ``id``
   */
  openInstance(row) {
    if (this.canOpen(row)) {
      this.env.navigate("instance_detail", {project_id: row.project_id, instance_id: Number(row.id)});
    }
  }

  /**
   * Whether an instance runs, in a word.
   *
   * @param {Object} row
   * @returns {string}
   */
  runText(row) {
    if (row.running === null || row.running === undefined) {
      return "—";
    }
    return row.running ? _t("running") : _t("stopped");
  }

  /**
   * Why the board has no line to draw, in words, from the server's
   * ``silence``.
   */
  get silenceText() {
    const silence = this.state.board?.silence || {};
    switch (silence.code) {
      case "agents_missing":
        return _t("The metrics agents are not installed on this server yet. The panel installs them on its own.");
      case "agents_failed":
        return _t("The metrics agents could not be installed on this server. The panel keeps trying on its own.");
      case "agents_waiting":
        return _t("The agents on this server are installed but have not sent anything yet. The first readings arrive within a few minutes.");
      case "host_silent":
        return _t("This server stopped sending metrics on %s. Check that it is on and reachable.", this.fmtAt(silence.since));
      default:
        return _t("Nothing was reported in the selected window. Pick a longer range or reset the zoom.");
    }
  }
}
