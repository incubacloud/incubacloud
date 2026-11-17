import {_t} from "@web/core/l10n/translation";
import {ChartBoard} from "./chart_board";
import {TimeSeriesChart} from "./time_series_chart";
import {formatNumber} from "./formatters";

/**
 * The fleet as the panel draws it, in Monitoring: how many servers
 * report and how many are short of disk, memory or time, the instances
 * deployed and running, the active alerts, the servers' security in
 * figures with the panel's own servers to look at, the ten busiest
 * servers by load, memory, root disk and disk activity, and which
 * servers went silent in the window.
 *
 * The servers are this panel's own, on every panel: the route reads
 * the central with the panel's account and names each server as the
 * panel does. One the reader can open (``own``) opens its own board.
 */
export class FleetBoard extends ChartBoard {
  static template = "incubacloud.FleetBoard";
  static components = {TimeSeriesChart};
  static props = {
    canConfigure: {type: Boolean, optional: true},
    onOpenHost: {type: Function, optional: true},
  };
  static defaultProps = {canConfigure: true};

  /** The route that answers the whole board. */
  get route() {
    return "/cloud/fleet/resource_series";
  }

  /** Whose servers the board shows, in words. */
  get scopeText() {
    return _t("Your servers");
  }

  /**
   * One panel's series, each server by its name; the Security chart's
   * two lines by what they count.
   *
   * @param {string} key a panel's key
   * @returns {Array<Object>}
   */
  series(key) {
    if (key === "security_ssh") {
      const kinds = {failed: _t("failed SSH logins"), bans: _t("addresses banned")};
      return super.series(key).map((s) => ({...s, label: kinds[s.role] || s.label}));
    }
    return super.series(key);
  }

  /**
   * The Security block of the answer: ``unavailable`` on a panel whose
   * servers may not send their security logs yet, ``ready`` with the
   * figures the panel keeps and its own servers to look at, or empty
   * from an answer without it.
   *
   * @returns {Object}
   */
  get security() {
    return this.state.board?.security || {};
  }

  /**
   * The Security figures, each with its tone: those the central counts
   * and those the panel counts from its own records.
   *
   * @returns {Array<{key: string, label: string, value: string, tone: string}>}
   */
  get securityTiles() {
    const v = this.panel("security").value || {};
    const sec = this.security;
    const count = (n) => (n === undefined ? "—" : formatNumber(n));
    const toneIf = (n, tone) => (n ? tone : "ok");
    return [
      {key: "ssh_failed_1h", label: _t("Failed SSH logins in the last hour"), value: count(v.ssh_failed_1h), tone: "ok"},
      {key: "ssh_bruteforce", label: _t("Servers under SSH brute force now"), value: count(v.ssh_bruteforce),
        tone: toneIf(v.ssh_bruteforce, "warn")},
      {key: "bans_1h", label: _t("Addresses banned in the last hour"), value: count(v.bans_1h), tone: "ok"},
      {key: "weblogin_bruteforce", label: _t("Instances under web-login brute force now"),
        value: count(v.weblogin_bruteforce), tone: toneIf(v.weblogin_bruteforce, "warn")},
      {key: "alerts_hosts", label: _t("Servers with security alerts"), value: count(sec.alerts_hosts),
        tone: toneIf(sec.alerts_hosts, "bad")},
      {key: "silent", label: _t("Servers whose security logs are not arriving"), value: count(v.silent),
        tone: toneIf(v.silent, "warn")},
      {key: "off", label: _t("Servers with security logs off"), value: count(sec.off), tone: toneIf(sec.off, "warn")},
      {key: "unhardened", label: _t("Servers without hardening"), value: count(sec.unhardened),
        tone: toneIf(sec.unhardened, "warn")},
    ];
  }

  /** The chart card of the Security block, its words translated here. */
  get securityCard() {
    return {
      key: "security_ssh", title: _t("Failed SSH logins and bans"), hint: _t("per hour · every server"),
      palette: "role", label: _t("Failed SSH logins and addresses banned on the servers per hour"),
      empty: _t("No failed SSH login on the servers in this window."),
    };
  }

  /** This panel's servers with something to look at, the worst first. */
  get securityRows() {
    return this.security.rows || [];
  }

  /** The line under the servers about those left out. */
  get securityHiddenText() {
    return _t("%s more servers with something to look at.", this.security.hidden || 0);
  }

  /**
   * What a server of the Security block has to look at, in words, each
   * with its tone.
   *
   * @param {Object} row a row of ``security.rows``
   * @returns {Array<{key: string, text: string, tone: string}>}
   */
  securityFlags(row) {
    const flags = [];
    if (row.alerts) {
      flags.push({key: "alerts", tone: row.level === "critical" ? "bad" : "warn",
        text: _t("Security alerts: %s", formatNumber(row.alerts))});
    }
    if (row.ssh_bruteforce) {
      flags.push({key: "ssh_bruteforce", tone: "warn",
        text: _t("SSH brute force: %s failed in 10 min", formatNumber(row.ssh_bruteforce))});
    }
    if (row.weblogin_bruteforce) {
      flags.push({key: "weblogin_bruteforce", tone: "warn",
        text: _t("Web-login brute force, instances: %s", formatNumber(row.weblogin_bruteforce))});
    }
    if (row.silent) {
      flags.push({key: "silent", tone: "warn", text: _t("Security logs not arriving")});
    }
    if (row.off) {
      flags.push({key: "off", tone: "muted", text: _t("Security logs off")});
    }
    if (row.unhardened) {
      flags.push({key: "unhardened", tone: "muted", text: _t("Not hardened")});
    }
    return flags;
  }

  /**
   * The figures over the charts, each with its tone.
   *
   * @returns {Array<{key: string, label: string, value: string, tone: string}>}
   */
  get tiles() {
    const v = this.panel("summary").value || {};
    const alerts = this.state.board?.alerts || {};
    const count = (key) => (v[key] === undefined ? "—" : formatNumber(v[key]));
    const toneIf = (n, tone) => (n ? tone : "ok");
    return [
      {key: "hosts", label: _t("Servers reporting"), value: count("hosts"), tone: "ok"},
      {key: "disk_full", label: _t("Root disk over 90 %"), value: count("disk_full"), tone: toneIf(v.disk_full, "bad")},
      {key: "memory_full", label: _t("Memory over 92 %"), value: count("memory_full"), tone: toneIf(v.memory_full, "warn")},
      {key: "pressure", label: _t("Waiting over 20 % of the time"), value: count("pressure"), tone: toneIf(v.pressure, "warn")},
      {key: "deployed", label: _t("Instances deployed"), value: count("deployed"), tone: "ok"},
      {key: "running", label: _t("Instances running"), value: count("running"), tone: "ok"},
      {key: "alerts_performance", label: _t("Active alerts"), value: formatNumber(alerts.performance || 0),
        tone: toneIf(alerts.performance, "warn")},
      {key: "alerts_security", label: _t("Active security alerts"), value: formatNumber(alerts.security || 0),
        tone: toneIf(alerts.security, "bad")},
    ];
  }

  /**
   * The chart cards of the board, their words translated here.
   *
   * @returns {Array<Object>}
   */
  get cards() {
    const empty = _t("No server reported in this window.");
    return [
      {key: "load", title: _t("Load per core"), hint: _t("ten busiest servers · above 1 they queue"),
        label: _t("Load per core of the ten busiest servers over time"), empty},
      {key: "memory", title: _t("Memory in use"), hint: _t("ten fullest servers"), unit: "percent",
        label: _t("Share of memory in use of the ten fullest servers over time"), empty},
      {key: "root", title: _t("Root disk in use"), hint: _t("ten fullest servers"), unit: "percent",
        label: _t("Share of the root disk in use of the ten fullest servers over time"), empty},
      {key: "io", title: _t("Disk busy"), hint: _t("ten busiest servers · busiest disk of each"), unit: "percent",
        label: _t("Share of the time the busiest disk of the ten busiest servers had work"), empty},
    ];
  }

  /** The rows of the ingestion line, silences first. */
  get timeline() {
    return this.panel("ingest").rows || [];
  }

  /** How many servers that reported throughout were left off the line. */
  get timelineHidden() {
    return this.panel("ingest").hidden || 0;
  }

  /** The line under the ingestion rows about the servers left off. */
  get timelineHiddenText() {
    return _t("%s more servers reported throughout the window.", this.timelineHidden);
  }

  /**
   * A segment of the ingestion line, placed in the window.
   *
   * @param {Object} seg ``{start, end, state}``, unix seconds
   * @returns {string} the inline style
   */
  segStyle(seg) {
    const {start, end} = this.window;
    const span = Math.max(1, end - start);
    const left = Math.max(0, ((seg.start - start) / span) * 100);
    const width = Math.max(0.4, ((Math.min(seg.end, end) - seg.start) / span) * 100);
    return `inset-inline-start:${left.toFixed(2)}%;inline-size:${width.toFixed(2)}%`;
  }

  /**
   * The tooltip of a segment: its state and when.
   *
   * @param {Object} seg ``{start, end, state}``
   * @returns {string}
   */
  segTitle(seg) {
    const at = (ts) => this.fmtAt(new Date(ts * 1000).toISOString());
    return seg.state === "ok"
      ? _t("Reporting from %(from)s to %(to)s", {from: at(seg.start), to: at(seg.end)})
      : _t("Silent from %(from)s to %(to)s", {from: at(seg.start), to: at(seg.end)});
  }

  /**
   * Whether a server of the board can open its own board: one the
   * reader can open on this panel, for whoever manages hosts.
   *
   * @param {Object} item a series or a row with ``own`` and ``host_id``
   * @returns {boolean}
   */
  canOpenHost(item) {
    return !!(item.own && item.host_id && this.env.permissions?.can_manage_hosts);
  }

  /**
   * Open a server's board: Monitoring's Hosts view when it handles it,
   * the server's Metrics tab otherwise.
   *
   * @param {Object} item a series or a row with ``host_id``
   */
  openHost(item) {
    const id = Number(item.host_id);
    if (this.props.onOpenHost) {
      this.props.onOpenHost(id);
    } else {
      this.env.navigate("host_detail", {host_id: id, tab: "metrics"});
    }
  }

  /** Why the board has no line to draw, in words. */
  get silenceText() {
    switch (this.state.board?.silence?.code) {
      case "no_hosts":
        return _t("You have no server yet. Add a VPS or connect a server of your own to start.");
      case "agents_missing":
        return _t("The metrics agents are not installed on any server yet. The panel installs them on its own.");
      default:
        return _t("Nothing was reported in the selected window. Pick a longer range or reset the zoom.");
    }
  }
}
