import {_t} from "@web/core/l10n/translation";
import {rpc} from "@web/core/network/rpc";
import {ChartBoard} from "./chart_board";
import {TimeSeriesChart} from "./time_series_chart";

/**
 * A server's Security view, beside Performance in its Metrics tab
 * (plan #84, C6): what is happening, whether the defence works, the
 * charts of the security counters, the HTTP of its instances and the
 * failed web logins.
 *
 * One call to ``/cloud/host/security_board`` per refresh brings it
 * whole. The server decides what each person gets: the figures for the
 * Developer role and above, the tables with addresses, users and
 * commands, and "see lines", only for whoever manages servers; an answer
 * without ``tables`` draws none, whatever this screen would allow.
 */
export class HostSecurityBoard extends ChartBoard {
  static template = "incubacloud.HostSecurityBoard";
  static components = {TimeSeriesChart};
  static props = {
    hostId: {type: Number},
    canConfigure: {type: Boolean, optional: true},
  };
  static defaultProps = {canConfigure: true};

  /**
   * The instance filter of the web-login tables and the lines shown.
   *
   * @returns {Object}
   */
  extraState() {
    return {
      instanceId: 0, lines: null, linesTitle: "", linesError: "", linesLoading: false,
      keyForm: null, keyError: "", keySaving: false, keyRemoving: 0,
    };
  }

  /** The route that answers the whole view. */
  get route() {
    return "/cloud/host/security_board";
  }

  /**
   * The server, and the instance the web-login tables are filtered by.
   *
   * @returns {Object}
   */
  routeParams() {
    const params = {host_id: this.props.hostId};
    if (this.state.instanceId) {
      params.instance_id = this.state.instanceId;
    }
    return params;
  }

  /** The state of the answer: ``ready``, ``unavailable`` or ``off``. */
  get viewState() {
    return this.state.board?.state || "ready";
  }

  /** Whether the answer carries the tables: only a manager's does. */
  get hasTables() {
    return !!this.state.board?.tables;
  }

  /**
   * One table of the answer, or an empty one.
   *
   * @param {string} key a table's key
   * @returns {Object}
   */
  table(key) {
    return this.state.board?.tables?.[key] || {rows: []};
  }

  /**
   * Why one table could not be read, in words, or "".
   *
   * @param {string} key a table's key
   * @returns {string}
   */
  tableError(key) {
    const error = this.state.board?.tables?.error || this.table(key).error;
    if (!error) {
      return "";
    }
    if (error === "no_route") {
      return _t("This panel's central has no log store the panel can read.");
    }
    return error === "busy" ? _t("The metrics central is busy.") : _t("The metrics central could not answer.");
  }

  /**
   * "Of the last 5 000 lines" under a table whose window held more.
   *
   * @param {string} key a table's key
   * @returns {string}
   */
  sampleText(key) {
    const t = this.table(key);
    return t.cut ? _t("Counted over the last %s lines of the window.", this.fmtNumber(t.sample || 0)) : "";
  }

  /** "· 61 different users" when the table names fewer than were tried. */
  get distinctUsersText() {
    const t = this.table("ssh_sources");
    const shown = (t.users || []).length;
    return t.distinct_users > shown ? _t("· %s different users", this.fmtNumber(t.distinct_users)) : "";
  }

  /**
   * The chart cards of one section, in order, their words translated
   * here so the card template stays generic.
   *
   * @param {string} section ``ssh``, ``activity`` or ``collector``
   * @returns {Array<Object>}
   */
  cardsOf(section) {
    const cards = {
      ssh: [
        {key: "ssh_failed", title: _t("Failed SSH connections"), hint: _t("per minute"),
          palette: "role", label: _t("Failed SSH connections to this server per minute"),
          empty: _t("No failed SSH connection to this server in this window.")},
        {key: "f2b_bans", title: _t("fail2ban bans"), hint: _t("per minute · by jail"),
          label: _t("Addresses fail2ban banned on this server per minute, by jail"),
          empty: _t("fail2ban banned no address in this window.")},
      ],
      activity: [
        {key: "accepted", title: _t("Sign-ins over SSH"), hint: _t("per hour · the panel's key or another"),
          label: _t("SSH sign-ins to this server per hour, by kind"),
          empty: _t("Nobody signed in over SSH in this window.")},
        {key: "sudo", title: _t("sudo commands"), hint: _t("per hour · by who"),
          label: _t("sudo commands on this server per hour, by kind"),
          empty: _t("No sudo command in this window.")},
        {key: "audit", title: _t("Changes to watched files"), hint: _t("per hour · by who"),
          label: _t("auditd records on the watched files per hour, by who"),
          empty: _t("No change to passwd, shadow, sudoers or the SSH configuration in this window.")},
        {key: "heartbeat", title: _t("Security lines"), hint: _t("per hour · by source"),
          label: _t("Security lines this server sent per hour, by source"),
          empty: _t("No security line arrived from this server in this window.")},
      ],
      collector: [
        {key: "collector", title: _t("Lines delivered and dropped"), hint: _t("per minute · to the central"),
          palette: "role", label: _t("Lines this server's collector delivered to the central and dropped per minute"),
          empty: _t("The collector reported nothing in this window.")},
        {key: "weblogin", title: _t("Failed web logins per instance"), hint: _t("per minute"),
          stacked: true, label: _t("Failed web logins to every instance of this server per minute"),
          empty: _t("No failed web login to the instances of this server in this window.")},
      ],
    };
    return cards[section] || [];
  }

  /**
   * One panel's series, with the words of the security counters' kinds
   * and the instances the panel could not name.
   *
   * @param {string} key a panel's key
   * @returns {Array<Object>}
   */
  series(key) {
    const kinds = {
      failed: _t("failed"),
      delivered: _t("delivered"),
      dropped: _t("dropped"),
      panel: _t("the panel"),
      other: _t("other"),
      ansible: _t("the panel (Ansible)"),
      panel_user: _t("the panel's user"),
      system: _t("system"),
      root: _t("root"),
      user: _t("another user"),
    };
    return super.series(key).map((s) => {
      if (key === "weblogin" && s.unidentified) {
        return {...s, name: _t("unidentified")};
      }
      if (key === "ssh_failed" || key === "collector") {
        return {...s, label: kinds[s.role] || s.label};
      }
      return {...s, name: kinds[s.name] || s.name};
    });
  }

  /** The diagnosis of the last answer, or an empty one. */
  get diagnosis() {
    return this.state.board?.diagnosis || {current: false, tone: "ok", findings: []};
  }

  /** The heading of the diagnosis card, by its tone. */
  get diagnosisHeading() {
    const tone = this.diagnosis.tone;
    if (tone === "bad") {
      return _t("Something needs a look");
    }
    return tone === "warn" ? _t("Under attack") : _t("Calm");
  }

  /** What the panel knows of the server's defence. */
  get defense() {
    return this.state.board?.defense || {};
  }

  /**
   * The items of "Is the defence working?", each with its tone.
   *
   * @returns {Array<{key: string, tone: string, text: string}>}
   */
  get defenseItems() {
    const d = this.defense;
    const items = [];
    items.push(d.ssh_allowlist
      ? {key: "ssh", tone: "ok", text: _t("SSH port: open to the panel and %s more addresses", d.ssh_allowlist)}
      : {key: "ssh", tone: "info", text: _t("SSH port: open to every address (fail2ban bans the attackers)")});
    if (!d.hardened) {
      items.push({key: "hardening", tone: "warn", text: _t("Hardening: never applied from the panel")});
    } else if (!d.hardening_current) {
      items.push({key: "hardening", tone: "warn", text: _t("Hardening: behind what the panel ships now")});
    } else {
      items.push({key: "hardening", tone: "ok", text: _t("Hardening: current")});
    }
    if (d.behind_cdn) {
      items.push(d.origin_closed
        ? {key: "cdn", tone: "ok", text: _t("Behind a CDN: the origin admits only the CDN")}
        : {key: "cdn", tone: "warn", text: _t("Behind a CDN: the origin also answers direct requests")});
    }
    items.push(d.enabled
      ? {key: "logs", tone: "ok", text: d.last_line_at
          ? _t("Security logs: sending (last line read %s)", this.fmtAt(d.last_line_at))
          : _t("Security logs: sending")}
      : {key: "logs", tone: "warn", text: _t("Security logs: off on this server")});
    if (d.write_user === "account") {
      items.push({key: "writer", tone: "warn", text: _t("Agents: they write with the account's credential, which also reads; their next install moves them to their own user")});
    } else if (d.write_user === "agent") {
      items.push({key: "writer", tone: "ok", text: _t("Agents: they write with a user that may only write")});
    }
    if (!d.key_known) {
      items.push({key: "key", tone: "info", text: _t("The panel signs in here without a key it can read: its sign-ins are told by user and address, not by key")});
    }
    return items;
  }

  /** The web logins' figures, without any address. */
  get weblogin() {
    return this.state.board?.weblogin || {instances: []};
  }

  /** "Over the last 24 h" or "7 d" of the web-login tables. */
  get webloginWindowText() {
    return this.weblogin.window_hours > 24 ? _t("last 7 days") : _t("last 24 hours");
  }

  /**
   * How many addresses the last web logins came from, in words.
   *
   * @returns {string}
   */
  get webloginDistinctText() {
    const w = this.weblogin;
    if (w.distinct === undefined || w.distinct === null) {
      return "";
    }
    const text = _t("%(distinct)s addresses · %(window)s", {distinct: this.fmtNumber(w.distinct), window: this.webloginWindowText});
    return w.cut ? text + " · " + _t("of the last %s attempts", this.fmtNumber(w.sample || 0)) : text;
  }

  /** The instances of this server the filter offers: those with attempts. */
  get filterInstances() {
    return this.weblogin.instances || [];
  }

  /**
   * Filter the web-login tables by one instance, or show every one.
   *
   * @param {Event} ev the select's change
   */
  onFilter(ev) {
    this.state.instanceId = Number(ev.target.value) || 0;
    this.load();
  }

  /** The rows of HTTP per instance, the unidentified attempts left out. */
  get httpRows() {
    return (this.panel("http").rows || []).filter((row) => !row.unidentified);
  }

  /**
   * An instance in a table: its name, or "unidentified".
   *
   * @param {Object} inst ``{id, name}``
   * @returns {string}
   */
  instanceLabel(inst) {
    return inst.name || _t("unidentified");
  }

  /**
   * The instances an address went to, in a line.
   *
   * @param {Array<Object>} instances ``[{id, name, attempts}]``
   * @returns {string}
   */
  instancesText(instances) {
    return (instances || []).map((i) => `${this.instanceLabel(i)} (${this.fmtNumber(i.attempts)})`).join(", ") || "—";
  }

  /**
   * Whether an address is banned now, in a word.
   *
   * @param {boolean|null} blocked
   * @returns {string}
   */
  blockedText(blocked) {
    if (blocked === null || blocked === undefined) {
      return "—";
    }
    return blocked ? _t("yes") : _t("no");
  }

  /**
   * A country code, or a dash; "local" for an address not on the internet.
   *
   * @param {string|null} code
   * @returns {string}
   */
  countryText(code) {
    if (!code) {
      return "—";
    }
    return code === "local" ? _t("local") : code;
  }

  /**
   * Whose a sign-in, a sudo or a change looks, in words.
   *
   * @param {string} code the server's attribution
   * @param {string} [label] the known key's name, for a sign-in with one
   * @returns {string}
   */
  attributionText(code, label) {
    if (code === "known_key") {
      return _t("known key: %s", label || "—");
    }
    if (code === "known_key_elsewhere") {
      return _t("known key «%s» from an address it may not sign in from", label || "—");
    }
    const words = {
      panel: _t("the panel"),
      panel_ansible: _t("the panel (Ansible)"),
      panel_elsewhere: _t("the panel's key from another address"),
      foreign_key: _t("another key"),
      password: _t("a password"),
      other_user: _t("another user"),
      outside: _t("outside every job and console of the panel"),
      system: _t("the system"),
    };
    return words[code] || code || "—";
  }

  /**
   * Whether an attribution needs a look.
   *
   * @param {string} code
   * @returns {boolean}
   */
  attributionBad(code) {
    return ["panel_elsewhere", "foreign_key", "password", "other_user", "outside", "known_key_elsewhere"].includes(code);
  }

  /**
   * Which key signed in, in words.
   *
   * @param {string} key ``panel``, ``other`` or ``none``
   * @returns {string}
   */
  keyText(key) {
    return {panel: _t("the panel's key"), known: _t("a known key"), other: _t("another key"), none: _t("no key")}[key] || "—";
  }

  /** The card of known keys, or null where the answer has none. */
  get knownKeys() {
    return this.state.board?.known_keys || null;
  }

  /** The known keys whose sign-ins raise no alert now. */
  get activeKeys() {
    return (this.knownKeys?.rows || []).filter((k) => k.active);
  }

  /** The keys removed, kept with who removed them and when. */
  get removedKeys() {
    return (this.knownKeys?.rows || []).filter((k) => !k.active);
  }

  /**
   * Who added a key and when, and its last use, in a line.
   *
   * @param {Object} k a row of the card
   * @returns {string}
   */
  keyAddedText(k) {
    const added = _t("Added by %(user)s on %(date)s", {user: k.added_by || "—", date: this.fmtAt(k.added_at)});
    if (!k.active) {
      return added;
    }
    const used = k.last_used_at ? _t("last used %s", this.fmtAt(k.last_used_at)) : _t("never used");
    return `${added} · ${used}`;
  }

  /**
   * Who removed a key and when, in a line.
   *
   * @param {Object} k a removed row of the card
   * @returns {string}
   */
  keyRemovedText(k) {
    return _t("Removed by %(user)s on %(date)s", {user: k.removed_by || "—", date: this.fmtAt(k.removed_at)});
  }

  /**
   * What removing a key does, asked on its row.
   *
   * @param {Object} k a row of the card
   * @returns {string}
   */
  removeKeyText(k) {
    return _t("Sign-ins with «%s» will raise an alert again.", k.name);
  }

  /** The heading of the removed keys, with how many (the card lists the last ones). */
  get removedKeysTitle() {
    return _t("Removed keys (%s)", this.knownKeys?.removed_count ?? this.removedKeys.length);
  }

  /**
   * The addresses a key may sign in from, in words.
   *
   * @param {Object} k a row of the card
   * @returns {string}
   */
  keyOnlyFromText(k) {
    if (!k.only_from) {
      return _t("any address");
    }
    return k.allowed_networks || _t("some addresses only");
  }

  /** Open the form that adds a key. */
  openKeyForm() {
    this.state.keyForm = {name: "", key: "", networks: ""};
    this.state.keyError = "";
  }

  /** Close the form without adding anything. */
  closeKeyForm() {
    this.state.keyForm = null;
    this.state.keyError = "";
  }

  /**
   * Keep what is typed in one field of the form.
   *
   * @param {string} field ``name``, ``key`` or ``networks``
   * @param {Event} ev the input's event
   */
  onKeyInput(field, ev) {
    this.state.keyForm[field] = ev.target.value;
  }

  /**
   * Answer a route of the known keys: the card again, or its error.
   *
   * @param {Object} res the route's answer
   * @returns {boolean} whether it was done
   */
  applyKeys(res) {
    if (!res?.ok) {
      this.state.keyError = res?.error || _t("Could not save the key.");
      return false;
    }
    if (this.state.board) {
      this.state.board.known_keys = res.known_keys;
    }
    this.state.keyError = "";
    return true;
  }

  /** Add the key of the form; the tables are read again with it. */
  async addKey() {
    const form = this.state.keyForm;
    if (!form || this.state.keySaving) {
      return;
    }
    this.state.keySaving = true;
    let res;
    try {
      res = await rpc("/cloud/host/known_key_add", {
        host_id: this.props.hostId, key: form.key, name: form.name, networks: form.networks,
      });
    } catch (e) {
      res = {ok: false, error: e?.data?.message || e?.message};
    }
    this.state.keySaving = false;
    if (this.applyKeys(res)) {
      this.state.keyForm = null;
      this.load();
    } else if (this.state.keyForm) {
      // What was pasted may have been a private key: refused, it does
      // not stay on the screen.
      this.state.keyForm.key = "";
    }
  }

  /**
   * Ask before removing a key, on its row.
   *
   * @param {number} id the key's id; 0 to stop asking
   */
  askRemoveKey(id) {
    this.state.keyRemoving = id;
    this.state.keyError = "";
  }

  /**
   * Remove a key: its sign-ins alert again, and it stays listed as removed.
   *
   * @param {number} id the key's id
   */
  async removeKey(id) {
    if (this.state.keySaving) {
      return;
    }
    this.state.keySaving = true;
    let res;
    try {
      res = await rpc("/cloud/host/known_key_remove", {host_id: this.props.hostId, key_id: id});
    } catch (e) {
      res = {ok: false, error: e?.data?.message || e?.message};
    }
    this.state.keySaving = false;
    if (this.applyKeys(res)) {
      this.state.keyRemoving = 0;
      this.load();
    }
  }

  /**
   * Who a change was made by, in words.
   *
   * @param {string} who ``system``, ``panel``, ``root`` or ``user``
   * @returns {string}
   */
  whoText(who) {
    return {system: _t("system"), panel: _t("the panel's user"), root: _t("root"), user: _t("another user")}[who] || who || "—";
  }

  /** The line under the view: where the countries come from. */
  get attributionLine() {
    return this.state.board?.attribution || "";
  }

  /**
   * Read the last lines of one kind ("see lines"): a manager's only, as
   * the route decides.
   *
   * @param {string} source ``sshd``, ``sudo``, ``fail2ban``, ``auditd``,
   *   ``weblogin`` or ``all``
   */
  async showLines(source) {
    const titles = {
      sshd: _t("SSH"),
      sudo: _t("sudo"),
      fail2ban: _t("fail2ban"),
      auditd: _t("auditd"),
      weblogin: _t("web logins"),
      all: _t("every source"),
    };
    this.state.linesTitle = titles[source] || source;
    this.state.linesLoading = true;
    this.state.linesError = "";
    this.state.lines = [];
    const params = {host_id: this.props.hostId, source, range_key: this.state.range === "7d" ? "7d" : "24h"};
    if (source === "weblogin" && this.state.instanceId) {
      params.instance_id = this.state.instanceId;
    }
    let res;
    try {
      res = await rpc("/cloud/host/security_lines", params);
    } catch (e) {
      res = {ok: false, error: e?.data?.message || e?.message || _t("Could not read the lines.")};
    }
    this.state.linesLoading = false;
    if (res.ok) {
      this.state.lines = res.lines;
    } else {
      this.state.linesError = res.error || _t("Could not read the lines.");
    }
  }

  /** Close the lines shown. */
  hideLines() {
    this.state.lines = null;
    this.state.linesError = "";
  }
}
