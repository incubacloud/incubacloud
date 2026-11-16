import {
  Component,
  useState,
  onWillStart,
  onMounted,
  onWillUnmount,
  useEffect,
  useEnv,
  useRef,
} from "@odoo/owl";
import {rpc} from "@web/core/network/rpc";
import {useService} from "@web/core/utils/hooks";
import {_t} from "@web/core/l10n/translation";
import {RepoEditor, gitlabReposWithoutHook} from "../repo_editor/repo_editor";
import {agoText, parseUTC} from "../../utils/dates";
import {useSafePoll} from "../../utils/use_safe_poll";
import {useVisibilityRefresh} from "../../utils/use_visibility_refresh";
import {useDebouncedBus, refreshesOn} from "../../utils/use_debounced_bus";
import {useNavGuard} from "../../utils/use_nav_guard";
import {announceJob} from "../../utils/job_queue";
import {pipFocusHint, usePipFocus} from "../../utils/pip_focus";
import {bareNameRelation, replaceReqLine} from "../../utils/pip_specs";
import {useFormValidation} from "../../utils/use_form_validation";
import {required, instanceName, portRange, when} from "../../utils/validators";
import {PasswordInput} from "../password_input/password_input";
import {TagSelector, tagStyle, tagDotStyle} from "../tag_selector/tag_selector";
import {IcConfirmDialog} from "../ic_confirm_dialog/ic_confirm_dialog";
import {IcModal} from "../ic_modal/ic_modal";
import {SearchSelect} from "../search_select/search_select";
import {RlSelect} from "../rl_select/rl_select";
import {restoreForm, SaveBar, unsavedCount} from "../settings_form/save_bar";
import {IcPager} from "../ic_pager/ic_pager";
import {TabBar} from "../tab_bar/tab_bar";
import {MetricsBoard} from "../metrics/metrics_board";
import {JobsMixin} from "./instance_detail_jobs";
import {MoveMixin} from "./instance_detail_move";
import {SyncMixin} from "./instance_detail_sync";
import {BackupsMixin} from "./instance_detail_backups";
import {AccessMixin} from "./instance_detail_access";
import {RestoreUploadMixin} from "./instance_detail_restore";
import {RefreshMixin} from "./instance_detail_refresh";
import {AuditMixin} from "./instance_detail_audit";
import {MailsMixin} from "./instance_detail_mails";
import {RelayMixin} from "./instance_detail_relay";
import { confirmVia } from "../../utils/use_confirm";
import {
  fetchOdooVersionFacts,
  newestSeries,
  pgDefaultFor,
  pgVersionsFor,
} from "../../utils/odoo_versions";

// ── Pip requirements helpers ─────────────────────────────────────────────────

function _parseReqLine(line) {
  const stripped = line.split("#")[0].trim();
  if (!stripped) return null;
  const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
  if (!m) return null;
  const name = m[1].toLowerCase();
  const markerM = stripped.match(/;(.+)/);
  const marker = markerM ? markerM[1].trim().toLowerCase() : "";
  return {name, raw: stripped, key: marker ? `${name}|${marker}` : name};
}

function _formatSource(url, branch) {
  const m = (url || "").match(/github\.com[:/](.+)/);
  const name = m ? m[1].replace(/\.git$/, "") : (url || "").replace(/\/$/, "");
  return branch ? `${name} (${branch})` : name;
}

function _buildPkgMap(text) {
  const pkgMap = {};
  const re =
    /<<<<<<< (?<eSrc>[^\n]*)\n(?<eLine>[^\n]*)\n=======\n(?<iLine>[^\n]*)\n>>>>>>> (?<iSrc>[^\n]*)/g;
  let m;
  while ((m = re.exec(text || "")) !== null) {
    const parsed = _parseReqLine(m.groups.eLine.trim());
    if (parsed) {
      pkgMap[parsed.key] = {
        type: "conflict",
        spec: parsed.raw,
        name: parsed.name,
        existingSrc: m.groups.eSrc.trim(),
        block: m[0],
      };
    }
  }
  const cleanOnly = (text || "").replace(
    /<<<<<<< [^\n]*\n[^\n]*\n=======\n[^\n]*\n>>>>>>> [^\n]*/g,
    ""
  );
  for (const line of cleanOnly.split("\n")) {
    const parsed = _parseReqLine(line);
    if (parsed && !(parsed.key in pkgMap)) {
      pkgMap[parsed.key] = {
        type: "clean",
        spec: parsed.raw,
        name: parsed.name,
        block: null,
      };
    }
  }
  return pkgMap;
}

/**
 * Merge incomingText into existingText with git-style conflict markers.
 * Returns { content, conflicts: [{name, existing, existingSource, incoming, incomingSource}] }
 */
function mergeRequirements(existingText, incomingText, repoUrl, repoBranch) {
  const sourceLabel = _formatSource(repoUrl, repoBranch);
  const pkgMap = _buildPkgMap(existingText);
  let newText = existingText || "";
  const added = [];
  const conflicts = [];

  for (const line of (incomingText || "").split("\n")) {
    const parsed = _parseReqLine(line);
    if (!parsed) continue;
    const {name, raw, key} = parsed;

    if (!(key in pkgMap)) {
      added.push(raw);
      pkgMap[key] = {type: "clean", spec: raw, name, block: null};
    } else if (pkgMap[key].spec === raw) {
      // exact duplicate — skip
    } else if (pkgMap[key].type === "conflict") {
      const oldBlock = pkgMap[key].block;
      const existingSrc = pkgMap[key].existingSrc;
      const existingSpec = pkgMap[key].spec;
      const newBlock = `<<<<<<< ${existingSrc}\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
      newText = newText.replace(oldBlock, newBlock);
      pkgMap[key].block = newBlock;
      conflicts.push({
        name,
        existing: existingSpec,
        existingSource: existingSrc,
        incoming: raw,
        incomingSource: sourceLabel,
      });
    } else if (bareNameRelation(pkgMap[key].spec, raw) === "existing") {
      // The stored line already meets a bare name — nothing to ask.
    } else {
      const existingSpec = pkgMap[key].spec;
      // The stored line as the parser reads it: blanks and a trailing
      // comment do not hide it.
      const isStored = (text) => {
        const stored = _parseReqLine(text);
        return Boolean(stored) && stored.key === key && stored.raw === existingSpec;
      };
      let replaced = false;
      if (bareNameRelation(existingSpec, raw) === "incoming") {
        // A bare name gives way to the line that says more.
        ({text: newText, replaced} = replaceReqLine(newText, isStored, raw, true));
      }
      if (replaced) {
        pkgMap[key] = {type: "clean", spec: raw, name, block: null};
        continue;
      }
      const newBlock = `<<<<<<< existing\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
      ({text: newText} = replaceReqLine(newText, isStored, newBlock));
      pkgMap[key] = {
        type: "conflict",
        spec: existingSpec,
        name,
        existingSrc: "existing",
        block: newBlock,
      };
      conflicts.push({
        name,
        existing: existingSpec,
        existingSource: "existing",
        incoming: raw,
        incomingSource: sourceLabel,
      });
    }
  }

  if (added.length) {
    const base = newText.trimEnd();
    newText = base + (base ? "\n" : "") + added.join("\n");
  }

  return {content: newText, conflicts};
}

/**
 * Apply user-chosen resolutions to pip_dependencies text.
 * choices: { [packageName]: chosenSpec }
 */
function applyPipResolutions(text, choices) {
  return (text || "").replace(
    /<<<<<<< [^\n]*\n([^\n]*)\n=======\n[^\n]*\n>>>>>>> [^\n]*/g,
    (block, existingLine) => {
      const parsed = _parseReqLine(existingLine.trim());
      return parsed && parsed.name in choices ? choices[parsed.name] : block;
    }
  );
}

/**
 * The sections of the new-instance page that start folded, as «Advanced»
 * (form standard §4): their defaults serve most instances, and the fields
 * in them are not required. A field to fix opens its section.
 */
export const NEW_FOLDED_SECTIONS = ["database", "email", "deps", "resources"];

/**
 * The tab of the form that holds each field, for the marks on the tab bar,
 * with the tags and dependencies the page keeps beside the form: the keys
 * of ``_editedValues()``, which the save bar counts. Fields no tab edits
 * (the environment, the series, the proxy) are left out.
 */
export const FIELD_TABS = {
  name: "general",
  host_id: "general",
  odoo_admin_password: "general",
  autopurge_exempt: "general",
  selectedTags: "general",
  postgres_version: "database",
  postgres_username: "database",
  postgres_password: "database",
  domains: "networking",
  website_info_public: "networking",
  smtp_relay_auth: "email",
  smtp_relay_host: "email",
  smtp_relay_port: "email",
  smtp_relay_security: "email",
  smtp_relay_user: "email",
  smtp_relay_password: "email",
  smtp_relay_oauth_directory_id: "email",
  smtp_relay_oauth_client_id: "email",
  smtp_relay_oauth_client_secret: "email",
  repos: "repos",
  pipDeps: "deps",
  aptDeps: "deps",
  odoo_conf: "config",
  backup_backend_id: "backups",
  auto_rebuild: "build",
  auto_update: "build",
  boot_test: "build",
  odoo_memory_limit: "resources",
  odoo_cpus: "resources",
  db_memory_limit: "resources",
  db_cpus: "resources",
  backup_memory_limit: "resources",
  backup_cpus: "resources",
  smtp_memory_limit: "resources",
  smtp_cpus: "resources",
  whitelist: "whitelist",
};

/**
 * The access log's summary before anything was read, every cut empty.
 *
 * @returns {Object}
 */
function emptyAccessSummary() {
  return {
    by_status: [], top_clients: [], top_paths: [], by_minute: [],
    login_posts: [], scan: {total: 0, top_clients: [], top_paths: []},
  };
}

// Responsibility mixins, split out in Fase 4 / B3. Order is
// irrelevant: no mixin overrides another's methods.
export class InstanceDetail extends JobsMixin(
  MoveMixin(
  SyncMixin(
  BackupsMixin(
  AccessMixin(
  RestoreUploadMixin(
  RefreshMixin(
  AuditMixin(
  MailsMixin(
  RelayMixin(
  Component)))))))))) {
  static props = {
    instance_id: {type: Number, optional: true},
    project_id: {type: Number},
    environment: {type: String, optional: true},
    embedded: {type: Boolean, optional: true},
    // ``{name}``: open the Dependencies tab with that package selected
    // (the pip conflict dialog's "Edit Manually").
    pipFocus: {type: Object, optional: true},
    // A tab to open, by its key (``?tab=``: the first steps' Add a domain
    // opens Networking); one this page does not show is ignored.
    tab: {type: String, optional: true},
  };
  static template = "incubacloud.InstanceDetail";
  static components = {
    RepoEditor,
    PasswordInput,
    TagSelector,
    IcConfirmDialog,
    IcModal,
    SearchSelect,
    RlSelect,
    IcPager,
    MetricsBoard,
    SaveBar,
    TabBar,
  };

  get isCreate() {
    return !this.props.instance_id;
  }

  /**
   * What «Auto-rebuild on push» cannot do: how many of the instance's
   * GitLab lines have no working webhook, said as the toggle shows it,
   * or "" when every line has one.
   *
   * @returns {string}
   */
  get gitlabHookWarning() {
    const count = gitlabReposWithoutHook(this.state.form.repos).length;
    if (!count) {
      return "";
    }
    return count === 1
      ? _t("1 GitLab repository without a working webhook: its pushes do not rebuild.")
      : _t("%(count)s GitLab repositories without a working webhook: their pushes do not rebuild.", {count});
  }

  /**
   * Whether the host can no longer be chosen in the form. Past draft the
   * instance's stack is on its host and its waiting jobs follow it: the
   * server takes a change from an administrator only, and "Move to host"
   * moves both.
   *
   * @returns {boolean}
   */
  get hostLocked() {
    return !this.isCreate && this.state.inst?.state !== "draft";
  }

  /**
   * Whether the backup backend can not be chosen in the form: where
   * backups are kept is a Developer's or an Administrator's choice, as
   * the server holds it.
   *
   * @returns {boolean}
   */
  get backupBackendLocked() {
    return !this.env.permissions?.can_manage_backups;
  }

  /** Rows per page for the server-paginated lists (backups, audit log). */
  get pageSize() {
    return 20;
  }

  get hasUnsavedChanges() {
    return this._savedForm && JSON.stringify(this._editedValues()) !== this._savedForm;
  }

  /**
   * What the person edits and a save sends: the form, and the tags and
   * dependencies kept beside it.
   *
   * @returns {object} one entry per thing the save bar counts
   */
  _editedValues() {
    return {
      ...this.state.form,
      selectedTags: this.state.selectedTags,
      pipDeps: this.state.pipDeps,
      aptDeps: this.state.aptDeps,
    };
  }

  /** @returns {number} how many things on the page are unsaved */
  get unsavedCount() {
    return this._savedForm
      ? unsavedCount(this._editedValues(), JSON.parse(this._savedForm))
      : 0;
  }

  /**
   * Put the page back as it was loaded or last saved. A repository's
   * requirements still being fetched no longer reach the dependencies.
   */
  discard() {
    if (!this._savedForm) return;
    this._discards = (this._discards || 0) + 1;
    const {selectedTags, pipDeps, aptDeps, ...form} = JSON.parse(this._savedForm);
    restoreForm(this.state.form, form);
    Object.assign(this.state, {selectedTags, pipDeps, aptDeps});
    this.validator.clearErrors();
    // The language box shows a name, not the id the form keeps.
    if (this.state.langs.length) {
      const lang = this.state.langs.find((l) => l.id === this.state.form.odoo_initial_lang);
      this.state.langSearch = lang ? `${lang.name} (${lang.code})` : "";
    }
  }

  /**
   * Take a saved repository's pin as saved: freezing and unfreezing
   * write it on the server at once.
   *
   * @param {number|null} repoId the repository's id; none for a new row
   * @param {string} sha the commit it is pinned to, "" when unpinned
   */
  _pinSaved(repoId, sha) {
    if (!repoId || !this._savedForm) return;
    const saved = JSON.parse(this._savedForm);
    const row = (saved.repos || []).find((r) => r.id === repoId);
    if (row) {
      row.commit_sha = sha;
      this._savedForm = JSON.stringify(saved);
    }
  }

  /**
   * Options for the host RlSelect: maps each available host to a
   * {value, label} entry where the label shows the host name plus its
   * free CPU / RAM, keeping numeric ids as the option value.
   * @returns {{value: number, label: string}[]} host select options
   */
  get hostOptions() {
    return this.state.hosts.map((h) => ({
      value: h.id,
      label: `${h.name} (${h.available_cpus} CPU / ${h.available_ram_gb} GB free)`,
    }));
  }

  /**
   * Options for the backup-backend RlSelect: maps each available backup
   * backend to a {value, label} entry (numeric id as value, name as label).
   * @returns {{value: number, label: string}[]} backup backend select options
   */
  get backupBackendOptions() {
    return this.state.backupBackends.map((bb) => ({
      value: bb.id,
      label: bb.name,
    }));
  }

  setup() {
    this.env = useEnv();
    this.orm = useService("orm");
    // Scroll box of the Recent requests table; reset on a page change.
    this.requestsBoxRef = useRef("requestsBox");
    // The new-instance page, where Create looks for the first field to fix.
    this.newPageRef = useRef("newPage");

    this.state = useState({
      tab: "overview",
      // The folded sections of the new-instance page opened by hand.
      openSections: {},
      // Bumped by a Create that found fields to fix: the first one takes
      // the focus once the page shows its error.
      focusErrorAsk: 0,
      loading: true,
      // The job whose "Action required" says why, opened by a click.
      holdOpen: null,
      // Each offered series' card (PostgreSQL range and default), from
      // the backend: the picker never hard-codes a version.
      versionFacts: {},
      saving: false,
      keeping: false,
      error: null,
      hosts: [],
      autoassignEnabled: false,
      backupBackends: [],
      langs: [],
      langsLoading: false,
      _initialLangId: null,
      inst: null,
      form: {},
      selectedTags: [],
      allTags: [],
      langSearch: "",
      langOpen: false,
      repoSearch: "",
      // Uncommitted text in the whitelist tab's input. Deliberately
      // outside ``form``: the dirty check compares the whole form, so
      // typing a hostname and thinking better of it would otherwise
      // leave the page claiming unsaved changes.
      newWhitelistEntry: "",
      pipDeps: "",
      aptDeps: "",
      pipConflictModal: null, // { conflicts, choices }
      // The package "Edit Manually" opened the Dependencies tab on.
      pipFocusName: null,
      pipFocusSeq: 0,
      // Metrics tab. The board reads the central itself; the access
      // log is read on demand, because it is live data from the host and
      // a stale copy would be worse than none during an incident.
      accessLog: {
        loading: false, loaded: false, error: "",
        entries: [], summary: emptyAccessSummary(),
        // Table view over the entries already read: the search text,
        // the status class chip ("all", "2".."5") and the page offset.
        q: "", cls: "all", offset: 0,
      },
      // Mails tab — a staging's captured mailbox. Read on demand for
      // the same reason as the access log: it is live data on the
      // host, and nothing about it is stored here.
      mails: {
        loading: false, loaded: false, error: "",
        items: [], total: 0,
        openId: null, message: null, messageLoading: false,
        messageError: "", view: "html",
      },
      // has_* flags — whether a password is currently stored server-side
      has_odoo_admin_password: false,
      has_postgres_password: false,
      has_smtp_relay_password: false,
      has_smtp_relay_oauth_client_secret: false,
      relayTestLoading: false,
      // sync modal
      syncModal: null, // { diff, actions, loading, applying, error }
      // action-related state
      actionLoading: false,
      confirmDialog: null,
      // move-to-host action
      movePicker: null, // { targetHostId, loading, loaded }
      moveHosts: [],
      // { sourceId, source, neutralize, loading, loaded, error, submitting }
      refreshDialog: null,
      refreshSources: [],
      conflictBlocked: null,
      restoreDialog: null,
      connectDialog: null,
      shellDialog: null,
      // activity (jobs)
      activeJobs: [],
      recentJobs: [],
      jobsTotal: 0,
      jobsLoading: false,
      // backups
      backupsLoading: false,
      backupsData: null,
      backupsError: null,
      backupsJobId: null,
      backupsOffset: 0,
      downloadModal: null, // { time, type, loading }
      createBackupModal: null, // { withFilestore, loading }
      restoreBackupModal: null, // { time, confirmName, loading }
      // audit log
      auditLog: [],
      auditLogLoading: false,
      auditOffset: 0,
      auditTotal: 0,
      auditFilter: {q: "", action: "", date_from: "", date_to: ""},
      auditActions: [],
      auditPurging: false,
    });

    this._pollTimer = null;
    this._savedForm = null;
    // How many times Discard ran: a requirements fetch started before
    // one leaves the dependencies alone.
    this._discards = 0;

    this.validator = useFormValidation(() => ({
      name: [required(_t("Instance name is required")), instanceName()],
      host_id: [
        when(
          () => !this.state.autoassignEnabled,
          required(_t("Host is required when auto-assign is disabled"))
        ),
      ],
      smtp_relay_port: [portRange(_t("Use a port between 1 and 65535."))],
    }));
    this._busService = useService("bus_service");
    // Shared guard for every recursive setTimeout(poll, …) in this
    // component. Registers onWillUnmount so zombie ticks can't
    // write into a destroyed component's state.
    this._safePoll = useSafePoll();

    // Debounced refresh of THIS instance's deeper detail
    // (jobs list, repos, etc. via /cloud/get_instance). The
    // project-level refresh — including sibling instances' status
    // dots — is owned by ``env.projectStore``, which subscribes
    // to the same bus once for the whole app. We do not call
    // ``refreshSidebar`` here: the store reacts to the same event
    // independently and the explicit hop would only duplicate the
    // /cloud/get_project_full RPC.
    const triggerRefresh = useDebouncedBus(() => {
      if (!this._destroyed) this._silentRefresh();
    });
    // The instance filter runs HERE, before the debounce — never
    // inside the callback. ``useDebouncedBus`` is last-write-wins on
    // its argument, so a matching event arriving in the same 300 ms
    // window as a non-matching one would be discarded if we branched
    // downstream. Filtering at the subscription keeps every event we
    // care about.
    //
    // The bus payload carries the target, so this used to cost an
    // ``orm.call("cloud.job", "load_jobs")`` per event — for every
    // job in the fleet, not just ours — purely to read
    // ``job.instance_id``. Same predicate, no round-trip.
    this._onJobUpdate = (payload) => {
      if (payload?.instance_id !== this.props.instance_id) return;
      if (!refreshesOn(payload)) return;
      triggerRefresh(payload.id);
    };

    usePipFocus(this);
    useEffect(
      (ask) => {
        if (ask) this._focusFirstError();
      },
      () => [this.state.focusErrorAsk]
    );
    onWillStart(() => this.load());
    onMounted(() => {
      if (!this.isCreate) {
        this._busService.subscribe("cloud_jobs", this._onJobUpdate);
        this._openAskedTab();
      }
    });
    onWillUnmount(() => {
      this._destroyed = true;
      this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
    });
    // Nav guard owns the ``beforeunload`` listener now — the old
    // direct listener was superseded to avoid duplicate handlers.
    useNavGuard(
      () => this.hasUnsavedChanges,
      (opts) => this._confirm(opts)
    );
    // Bus covers the happy path; ``visibilitychange`` refresh catches
    // events dropped while the tab was backgrounded.
    useVisibilityRefresh(() => {
      if (!this.isCreate && !this._destroyed) this._silentRefresh();
    });
  }

  async _silentRefresh() {
    try {
      const inst = await rpc("/cloud/get_instance", {
        instance_id: this.props.instance_id,
      });
      const firstDeploy = !this.state.inst?.deployed && inst.deployed;
      this.state.inst = inst;
      this.state.activeJobs = inst.activeJobs || [];
      this.state.recentJobs = inst.recentJobs || [];
      this.state.jobsTotal = inst.jobsTotal || 0;
      // A production's Backups tab, open before its first deploy, only
      // offered the destination: once deployed it has a list to show.
      if (
        firstDeploy && this.state.tab === "backups"
        && !this.state.backupsData && !this.state.backupsLoading
      ) {
        this._loadBackups();
      }
      // Patch the project store so the sidebar reflects this
      // instance's fresh status without waiting for its own bus
      // refresh tick.
      this.env.projectStore?.updateInstance(this.props.instance_id, inst);
    } catch (_e) {
      console.debug("Silent refresh failed:", _e);
    }
  }

  timeAgo(dateStr) {
    if (!dateStr) return "";
    return agoText(Math.floor((new Date() - parseUTC(dateStr)) / 1000));
  }

  /**
   * How many aliases the instance has besides its main domain.
   *
   * @param {number} n the aliases.
   * @returns {string} "1 additional", "3 additional"
   */
  aliasesLabel(n) {
    return n === 1 ? _t("1 additional") : _t("%s additional", n);
  }

  formatDate(dateStr) {
    if (!dateStr) return "";
    const d = parseUTC(dateStr);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
      d.getHours()
    )}:${pad(d.getMinutes())}`;
  }

  goResolveAlert(ev) {
    ev.stopPropagation();
    this.env.toggleSlideOver("alerts");
  }

  _applyInstanceData(inst, hosts, backends) {
    this.state.hosts = (hosts || []).filter(
      (h) => h.status === "compatible" && h.traefik_deployed
    );
    this.state.backupBackends = backends || [];
    this.state.inst = inst;
    this.state.activeJobs = inst.activeJobs || [];
    this.state.recentJobs = inst.recentJobs || [];
    this.state.jobsTotal = inst.jobsTotal || 0;
    if (inst.odoo_initial_lang) {
      this.state._initialLangId = inst.odoo_initial_lang;
      this.state.langSearch = "(loading…)";
    }
    this.state.has_odoo_admin_password = inst.has_odoo_admin_password || false;
    this.state.has_postgres_password = inst.has_postgres_password || false;
    this.state.has_smtp_relay_password = inst.has_smtp_relay_password || false;
    this.state.has_smtp_relay_oauth_client_secret =
      inst.has_smtp_relay_oauth_client_secret || false;
    this.state.pipDeps = inst.pip_dependencies || "";
    this.state.aptDeps = inst.apt_dependencies || "";
    this.state.form = {
      name: inst.name || "",
      environment: inst.environment || "staging",
      host_id: inst.host_id || null,
      odoo_version: inst.odoo_version || newestSeries(this.state.versionFacts),
      odoo_initial_lang: inst.odoo_initial_lang || null,
      odoo_admin_password: "",
      odoo_proxy: inst.odoo_proxy || "traefik",
      postgres_version: inst.postgres_version || "17",
      postgres_dbname: inst.postgres_dbname || "",
      postgres_username: inst.postgres_username || "",
      postgres_password: "",
      domains: (inst.domains || []).map((d) => ({...d})),
      smtp_relay_host: inst.smtp_relay_host || "",
      smtp_relay_port: inst.smtp_relay_port || 587,
      smtp_relay_security: inst.smtp_relay_security || "starttls",
      smtp_relay_user: inst.smtp_relay_user || "",
      smtp_relay_password: "",
      smtp_relay_auth: inst.smtp_relay_auth || "password",
      smtp_relay_oauth_directory_id: inst.smtp_relay_oauth_directory_id || "",
      smtp_relay_oauth_client_id: inst.smtp_relay_oauth_client_id || "",
      smtp_relay_oauth_client_secret: "",
      odoo_conf: inst.odoo_conf || "",
      odoo_memory_limit: inst.odoo_memory_limit || "2g",
      odoo_cpus: inst.odoo_cpus || 2.0,
      db_memory_limit: inst.db_memory_limit || "1g",
      db_cpus: inst.db_cpus || 1.0,
      backup_memory_limit: inst.backup_memory_limit || "512m",
      backup_cpus: inst.backup_cpus || 0.5,
      smtp_memory_limit: inst.smtp_memory_limit || "256m",
      smtp_cpus: inst.smtp_cpus || 0.25,
      backup_backend_id: inst.backup_backend_id || null,
      auto_rebuild: inst.auto_rebuild || false,
      autopurge_exempt: inst.autopurge_exempt || false,
      website_info_public: inst.website_info_public || false,
      auto_update: inst.auto_update !== false,
      boot_test: inst.boot_test !== false,
      repos: (inst.repos || []).map((r) => ({...r})),
      whitelist: [...(inst.whitelist || [])],
    };
    this.state.selectedTags = [...(inst.tags || [])];
    this.state.allTags = [...(inst.all_tags || [])];
    this.state.backupsData = null;
    this.state.backupsError = null;
    this._savedForm = JSON.stringify(this._editedValues());
  }

  /**
   * The PostgreSQL versions the form's Odoo series runs on.
   *
   * @returns {string[]}
   */
  get pgVersions() {
    return pgVersionsFor(this.state.versionFacts, this.state.form?.odoo_version);
  }

  async load() {
    this.state.loading = true;
    try {
      // The series' cards come first: the form's defaults read them.
      this.state.versionFacts = await fetchOdooVersionFacts();
      if (this.isCreate) {
        this.state.tab = "general";
        const [hostsResp, project, backends] = await Promise.all([
          rpc("/cloud/get_hosts"),
          rpc("/cloud/get_project", {project_id: this.props.project_id}),
          rpc("/cloud/get_backup_backends", {}),
        ]);
        this.state.backupBackends = backends?.items || backends || [];
        const allHosts = hostsResp.hosts || hostsResp || [];
        this.state.autoassignEnabled = !!hostsResp.autoassign_enabled;
        this.state.hosts = allHosts.filter(
          (h) => h.status === "compatible" && h.traefik_deployed
        );
        // ``from_project``: below Project Manager their URL is read-only,
        // as on a saved line (RepoEditor's ``urlLocked``).
        const repos = (project?.repos || []).map((r) => ({
          id: null,
          from_project: true,
          url: r.url || "",
          branch: r.branch || "main",
          addons: "",
          excludes: "",
          commit_sha: r.commit_sha || "",
        }));
        this.state.pipDeps = project?.pip_dependencies || "";
        this.state.aptDeps = project?.apt_dependencies || "";
        try {
          const tagsRes = await rpc("/cloud/get_tags", {scope: "instance"});
          this.state.allTags = tagsRes.tags || [];
        } catch (_e) {
          /* noop */
        }
        this.state.form = {
          name: "",
          // Production until the project has one: a staging is a copy
          // of it, and a customer used to environments that start on a
          // development copy picked staging for their only instance.
          environment: this.props.environment
            || (project?.has_production ? "staging" : "production"),
          host_id: null,
          odoo_version: parseFloat(project?.odoo_version)
            || newestSeries(this.state.versionFacts),
          odoo_initial_lang: null,
          odoo_admin_password: "",
          odoo_proxy: "traefik",
          postgres_version: pgDefaultFor(this.state.versionFacts, project?.odoo_version),
          postgres_username: "odoo",
          postgres_password: "",
          domains: [],
          smtp_relay_host: "",
          smtp_relay_port: 587,
          smtp_relay_security: "starttls",
          smtp_relay_user: "",
          smtp_relay_password: "",
          smtp_relay_auth: "password",
          smtp_relay_oauth_directory_id: "",
          smtp_relay_oauth_client_id: "",
          smtp_relay_oauth_client_secret: "",
          odoo_conf: "",
          odoo_memory_limit: "2g",
          odoo_cpus: 2.0,
          db_memory_limit: "1g",
          db_cpus: 1.0,
          backup_memory_limit: "512m",
          backup_cpus: 0.5,
          smtp_memory_limit: "256m",
          smtp_cpus: 0.25,
          backup_backend_id: project?.backup_backend_id || null,
          auto_rebuild: false,
          autopurge_exempt: false,
          website_info_public: false,
          auto_update: true,
          boot_test: true,
          repos,
          // Seeded on the server from the chosen host, so the create
          // form has nothing to show and nothing to send.
          whitelist: [],
        };
        this._savedForm = JSON.stringify(this._editedValues());
      } else {
        // Try the project store first — instant switch when
        // navigating between instances of the same project.
        const storeState = this.env.projectStore?.state;
        if (storeState?.projectId === this.props.project_id && storeState.data) {
          const cached = storeState.data.instances[this.props.instance_id];
          if (cached) {
            this._applyInstanceData(
              cached,
              storeState.data.hosts,
              storeState.data.backup_backends
            );
            this.state.loading = false;
            return;
          }
        }
        // Store miss — fall back to server.
        const inst = await rpc("/cloud/get_instance", {
          instance_id: this.props.instance_id,
        });
        this._applyInstanceData(inst, this.state.hosts, this.state.backupBackends);
      }
    } catch (e) {
      this.state.error = this.isCreate
        ? _t("Failed to load project data.")
        : _t("Failed to load instance.");
    } finally {
      this.state.loading = false;
    }
  }

  /**
   * A tab's name in the bar.
   *
   * @param {string} key the tab's key
   * @returns {{key: string, label: string}}
   */
  _tab(key) {
    const labels = {
      overview: _t("Overview"),
      general: _t("General"),
      database: _t("Database"),
      networking: _t("Networking"),
      email: _t("Email"),
      repos: _t("Repos"),
      config: _t("Odoo Config"),
      backups: _t("Backups"),
      mails: _t("Mails"),
      metrics: _t("Metrics"),
      deps: _t("Dependencies"),
      build: _t("Build"),
      resources: _t("Resources"),
      whitelist: _t("Whitelist"),
      activity: _t("Audit Logs"),
    };
    return {key, label: labels[key]};
  }

  /**
   * The tabs of the instance's page: those in the bar and those under
   * «More». At most eight in the bar (form standard §1); a tab is always
   * in the same place whatever the environment and the permissions. Email
   * is production's and Mails staging's, so each takes the other's place.
   * The new-instance page has no bar: these are its sections, one under
   * the other (``newSections``).
   *
   * @returns {{tabs: {key: string, label: string}[], more: {key: string, label: string}[]}}
   */
  get instanceTabs() {
    const perms = this.env.permissions || {};
    const production = this.state.form.environment === "production";
    if (this.isCreate) {
      const keys = ["general", "database", "networking"];
      if (production) keys.push("email");
      keys.push("repos", "deps", "resources");
      if (this.canShowBackups) keys.push("backups");
      return {tabs: keys.map((k) => this._tab(k)), more: []};
    }
    const keys = ["overview"];
    const more = [];
    if (perms.can_edit_instance) {
      keys.push("general", "networking");
      if (production) keys.push("email");
      keys.push("repos", "config");
      if (this.canShowBackups) keys.push("backups");
      more.push("deps", "build", "resources", "database");
      if (this.canShowWhitelist) more.push("whitelist");
    }
    if (this.canShowMails) keys.push("mails");
    if (perms.can_view_metrics) keys.push("metrics");
    if (perms.can_manage_hosts) more.push("activity");
    return {tabs: keys.map((k) => this._tab(k)), more: more.map((k) => this._tab(k))};
  }

  /**
   * The tabs holding a change not saved yet or a field to fix, by the
   * field each one holds (``FIELD_TABS``). A change is told against what
   * was loaded or last saved, as the save bar counts it.
   *
   * @returns {string[]} their keys
   */
  get markedTabs() {
    const changed = this._fieldChanged();
    return Object.keys(this._fieldsByTab(
      (field) => changed(field) || this.validator.fieldError(field)
    ));
  }

  /**
   * A test of whether a field differs from what was loaded or last saved.
   *
   * @returns {(field: string) => boolean}
   */
  _fieldChanged() {
    const saved = this._savedForm ? JSON.parse(this._savedForm) : null;
    const edited = this._editedValues();
    return (field) => !!saved
      && JSON.stringify(edited[field]) !== JSON.stringify(saved[field]);
  }

  /**
   * How many fields each tab holds that the test picks, by the field each
   * one holds (``FIELD_TABS``).
   *
   * @param {(field: string) => *} picks truthy for a field to count
   * @returns {Object<string, number>} the count of each tab that has any
   */
  _fieldsByTab(picks) {
    const counts = {};
    for (const [field, tab] of Object.entries(FIELD_TABS)) {
      if (picks(field)) counts[tab] = (counts[tab] || 0) + 1;
    }
    return counts;
  }

  /**
   * The sections of the new-instance page: the tabs the form has when
   * creating, in their order and with their names, each saying whether it
   * starts folded (``NEW_FOLDED_SECTIONS``).
   *
   * @returns {{key: string, label: string, folded: boolean}[]}
   */
  get newSections() {
    return this.instanceTabs.tabs.map((tab) => ({
      ...tab,
      folded: NEW_FOLDED_SECTIONS.includes(tab.key),
    }));
  }

  /**
   * Whether a section of the new-instance page shows its fields: always
   * for one that does not fold; for a folded one, once opened by hand or
   * while a field in it needs fixing.
   *
   * @param {string} key the section's key, a tab's
   * @returns {boolean}
   */
  sectionOpen(key) {
    if (!NEW_FOLDED_SECTIONS.includes(key) || this.state.openSections[key]) {
      return true;
    }
    return key in this._fieldsByTab((field) => this.validator.fieldError(field));
  }

  /**
   * Fold or unfold a folded section of the new-instance page. A section
   * with a field to fix stays open.
   *
   * @param {string} key the section's key
   */
  toggleSection(key) {
    this.state.openSections[key] = !this.sectionOpen(key);
  }

  /**
   * How many fields of a section differ from what the new-instance page
   * started with, so a folded section says how many it holds changed.
   *
   * @param {string} key the section's key
   * @returns {number}
   */
  sectionChangedCount(key) {
    return this._fieldsByTab(this._fieldChanged())[key] || 0;
  }

  /**
   * What a folded section of the new-instance page says of its changed
   * fields, empty when it holds none.
   *
   * @param {string} key the section's key
   * @returns {string}
   */
  sectionChangedText(key) {
    const count = this.sectionChangedCount(key);
    if (!count) return "";
    return count === 1 ? _t("1 changed") : _t("%s changed", count);
  }

  /** @returns {number} how many fields of the form show an error */
  get newErrorCount() {
    return Object.keys(FIELD_TABS).filter((field) => this.validator.fieldError(field)).length;
  }

  /** @returns {string} what the new-instance footer says of those fields */
  get newErrorText() {
    const count = this.newErrorCount;
    return count === 1
      ? _t("1 field needs attention")
      : _t("%s fields need attention", count);
  }

  /**
   * Take the focus to the first field of the new-instance page that shows
   * an error, in the page's order, and bring it into view.
   */
  _focusFirstError() {
    const field = this.newPageRef.el?.querySelector('[aria-invalid="true"]');
    if (!field) return;
    field.focus();
    field.scrollIntoView?.({block: "center"});
  }

  /** Open the tab the address asks for (``tab``), when this page shows it. */
  _openAskedTab() {
    const {tabs, more} = this.instanceTabs;
    if (this.props.tab && [...tabs, ...more].some((t) => t.key === this.props.tab)) {
      this.setTab(this.props.tab);
    }
  }

  setTab(tab) {
    this.state.tab = tab;
    if (tab !== "deps") this.state.pipFocusName = null;
    // Before its first deploy a production has no backup to list yet:
    // the tab only offers to choose where they will go.
    if (
      tab === "backups" && this.state.inst?.deployed
      && !this.state.backupsData && !this.state.backupsLoading
    ) {
      this._loadBackups();
    }
    if (
      tab === "activity" &&
      !this.state.auditLog.length &&
      !this.state.auditLogLoading
    ) {
      this._loadAuditLog();
    }
    if (tab === "metrics") {
      if (!this.state.accessLog.loaded && !this.state.accessLog.loading) {
        this.loadAccessLog();
      }
    }
    if (tab === "mails" && !this.state.mails.loaded && !this.state.mails.loading) {
      this.loadMails();
    }
  }

  /**
   * True when this panel's own operator edits the observability
   * settings, so a stub pointing at Settings is actionable advice.
   *
   * @see Monitoring#canConfigure — same axis, same reason for reading it
   * from the boot config rather than from the metrics route.
   */
  get canConfigureObservability() {
    return this.env.features?.observability?.configure !== false;
  }

  /**
   * Read this instance's recent requests from the proxy on its host.
   *
   * Answers what the charts cannot: which client IPs are hitting it and
   * on which paths. Nothing is cached server-side, so every refresh is
   * a live read.
   */
  async loadAccessLog() {
    this.state.accessLog.loading = true;
    this.state.accessLog.error = "";
    this.state.accessLog.offset = 0;
    try {
      const res = await rpc("/cloud/instance/access_log", {
        instance_id: this.props.instance_id,
      });
      if (!res.ok) {
        this.state.accessLog.error = res.error || _t("Could not read the proxy log.");
        this.state.accessLog.entries = [];
      } else {
        this.state.accessLog.entries = res.entries || [];
        this.state.accessLog.summary = {...emptyAccessSummary(), ...(res.summary || {})};
      }
    } catch (e) {
      this.state.accessLog.error =
        e?.data?.message || e?.message || _t("Could not read the proxy log.");
      this.state.accessLog.entries = [];
    } finally {
      this.state.accessLog.loading = false;
      this.state.accessLog.loaded = true;
    }
  }

  /** Bring the recent requests of the Metrics tab into view. */
  showRequests() {
    document.getElementById("ic-requests")?.scrollIntoView({behavior: "smooth", block: "start"});
  }

  /**
   * The busiest minute of the requests read, for scaling the per-minute
   * bars.
   */
  get requestsPeakMinute() {
    return Math.max(1, ...(this.state.accessLog.summary.by_minute || []).map((m) => m.count));
  }

  /** Most recent requests first, which is how an incident is read. */
  get recentRequests() {
    return [...(this.state.accessLog.entries || [])].reverse();
  }

  /** Rows per page of the Recent requests table. */
  get requestsPageSize() {
    return 50;
  }

  /**
   * Status class of a log entry.
   * @param {Object} req access log entry
   * @returns {string} "2", "3", "4" or "5"; "" when the status is not a code
   */
  requestClass(req) {
    const first = String(req.DownstreamStatus ?? "").charAt(0);
    return "2345".includes(first) && first ? first : "";
  }

  /** Lower-cased search text of the Recent requests table. */
  get requestsQuery() {
    return (this.state.accessLog.q || "").trim().toLowerCase();
  }

  /**
   * Requests matching the search text, before the status chip applies.
   * The text is looked for in the client, method, path and status code,
   * ignoring case.
   * @returns {Object[]} matching entries, newest first
   */
  get searchedRequests() {
    const q = this.requestsQuery;
    if (!q) {
      return this.recentRequests;
    }
    return this.recentRequests.filter((req) =>
      [req.ClientHost, req.RequestPath, req.RequestMethod, req.DownstreamStatus]
        .some((field) => String(field ?? "").toLowerCase().includes(q))
    );
  }

  /**
   * Status chips with the number of searched requests each one holds, so
   * the counts follow the search.
   * @returns {{key: string, label: string, count: number}[]} chips
   */
  get requestChips() {
    const searched = this.searchedRequests;
    const count = (key) =>
      searched.filter((req) => this.requestClass(req) === key).length;
    return [
      { key: "all", label: _t("All"), count: searched.length },
      ...["2", "3", "4", "5"].map((key) => ({
        key, label: `${key}xx`, count: count(key),
      })),
    ];
  }

  /**
   * Requests matching both the search text and the status chip.
   * @returns {Object[]} filtered entries, newest first
   */
  get filteredRequests() {
    const cls = this.state.accessLog.cls;
    const searched = this.searchedRequests;
    return cls === "all"
      ? searched
      : searched.filter((req) => this.requestClass(req) === cls);
  }

  /**
   * The page of filtered requests the table shows.
   * @returns {Object[]} at most ``requestsPageSize`` entries
   */
  get pagedRequests() {
    const {offset} = this.state.accessLog;
    return this.filteredRequests.slice(offset, offset + this.requestsPageSize);
  }

  /**
   * Footer count: "N requests" when nothing is filtered out, otherwise
   * "N of M requests". A total of one has strings of its own, as the
   * panel words a singular elsewhere.
   * @returns {string} translated count
   */
  get requestsCountLabel() {
    const shown = this.filteredRequests.length;
    const total = this.recentRequests.length;
    if (shown === total) {
      return total === 1
        ? _t("1 request")
        : _t("%(count)s requests", {count: total});
    }
    return total === 1
      ? _t("%(count)s of 1 request", {count: shown})
      : _t("%(count)s of %(total)s requests", {count: shown, total});
  }

  /**
   * Split a cell's text around the first occurrence of the search text so
   * the template can wrap the hit in ``<mark>`` without building HTML.
   * @param {*} value cell value
   * @returns {string[]} [before, hit, after]; hit is empty without a match
   */
  highlightRequest(value) {
    const text = String(value ?? "");
    const q = this.requestsQuery;
    const at = q ? text.toLowerCase().indexOf(q) : -1;
    if (at < 0) {
      return [text, "", ""];
    }
    return [text.slice(0, at), text.slice(at, at + q.length), text.slice(at + q.length)];
  }

  /**
   * Search the Recent requests table and go back to its first page.
   * @param {Event} ev input event of the search box
   */
  onRequestsSearch(ev) {
    this.state.accessLog.q = ev.target.value;
    this.state.accessLog.offset = 0;
  }

  /**
   * Filter the Recent requests table by status class and go back to its
   * first page.
   * @param {string} key "all", "2", "3", "4" or "5"
   */
  setRequestsClass(key) {
    this.state.accessLog.cls = key;
    this.state.accessLog.offset = 0;
  }

  /**
   * Change page in the Recent requests table and scroll its box to the top.
   * @param {number} offset index of the first request of the new page
   */
  requestsGoPage(offset) {
    this.state.accessLog.offset = offset;
    const box = this.requestsBoxRef?.el;
    if (box) {
      box.scrollTop = 0;
    }
  }

  onInput(field, ev) {
    this.state.form[field] = ev.target.value;
  }

  onDepsInput(ev) {
    this.state.pipDeps = ev.target.value;
  }

  onAptInput(ev) {
    this.state.aptDeps = ev.target.value;
  }

  onPasswordChange(field, value) {
    this.state.form[field] = value;
  }

  /**
   * The Dependencies tab's line about the package "Edit Manually" selected.
   *
   * @returns {string}
   */
  get pipFocusHint() {
    return pipFocusHint(this.state.pipFocusName);
  }

  // ── Pip conflict modal ───────────────────────────────────────────────────

  openPipConflictModal(conflicts) {
    const choices = {};
    for (const c of conflicts) {
      choices[c.name] = c.existing;
    }
    this.state.pipConflictModal = {conflicts, choices};
  }

  closePipConflictModal() {
    this.state.pipConflictModal = null;
  }

  setPipChoice(name, spec) {
    if (this.state.pipConflictModal) {
      this.state.pipConflictModal.choices[name] = spec;
    }
  }

  confirmPipResolve() {
    const {choices} = this.state.pipConflictModal;
    this.state.pipConflictModal = null;
    this.state.pipDeps = applyPipResolutions(this.state.pipDeps, choices);
  }

  onHostSelect(ev) {
    const id = parseInt(ev.target.value);
    this.state.form.host_id = id || null;
  }

  onVersionSelect(ev) {
    this.state.form.odoo_version = parseFloat(ev.target.value);
    // A PostgreSQL the new series does not run on gives way to its default.
    if (!this.pgVersions.includes(this.state.form.postgres_version)) {
      this.state.form.postgres_version = pgDefaultFor(
        this.state.versionFacts, this.state.form.odoo_version,
      );
    }
  }

  onPgSelect(ev) {
    this.state.form.postgres_version = ev.target.value;
  }

  // ── Domain management ──────────────────────────────────────────────

  /**
   * Certificate choices for a domain row, labelled in the user's language.
   * Values match `cloud.instance.domain.cert_resolver`; "auto" comes first
   * because it is the model's default and what every row holds unless
   * somebody chose otherwise.
   */
  get certResolverOptions() {
    return [
      {value: "auto", label: _t("Automatic")},
      {value: "letsencrypt", label: _t("Let's Encrypt")},
      {value: "custom", label: _t("Existing certificate")},
      {value: "none", label: _t("No TLS")},
    ];
  }

  addDomain() {
    this.state.form.domains.push({
      id: null,
      hostname: "",
      redirect_to: "",
      cert_resolver: "auto",
      redirect_permanent: false,
    });
  }

  removeDomain(idx) {
    this.state.form.domains.splice(idx, 1);
  }

  onDomainChange(idx, field, value) {
    this.state.form.domains[idx][field] = value;
  }

  onBackupBackendSelect(ev) {
    const val = ev.target.value;
    this.state.form.backup_backend_id = val ? parseInt(val) : null;
  }

  async _ensureLangs() {
    if (this.state.langs.length) return;
    this.state.langsLoading = true;
    try {
      const langs = await rpc("/cloud/get_langs");
      this.state.langs = langs;
      if (this.state._initialLangId) {
        const found = langs.find((l) => l.id === this.state._initialLangId);
        this.state.langSearch = found ? `${found.name} (${found.code})` : "";
        this.state._initialLangId = null;
      }
    } finally {
      this.state.langsLoading = false;
    }
  }

  get filteredLangs() {
    const q = this.state.langSearch.toLowerCase();
    const list = q
      ? this.state.langs.filter(
          (l) => l.name.toLowerCase().includes(q) || l.code.toLowerCase().includes(q)
        )
      : this.state.langs;
    return list.slice(0, 10);
  }

  get filteredRepos() {
    const q = (this.state.repoSearch || "").toLowerCase();
    const repos = this.state.form.repos || [];
    if (!q) return repos.map((r, i) => ({...r, _origIdx: i}));
    return repos
      .map((r, i) => ({...r, _origIdx: i}))
      .filter((r) => (r.url || "").toLowerCase().includes(q));
  }

  onLangBlur() {
    setTimeout(() => {
      this.state.langOpen = false;
    }, 150);
  }

  onLangSearchInput(ev) {
    this.state.langSearch = ev.target.value;
    this.state.langOpen = true;
    if (!ev.target.value) {
      this.state.form.odoo_initial_lang = null;
    }
  }

  selectLang(lang) {
    if (lang) {
      this.state.form.odoo_initial_lang = lang.id;
      this.state.langSearch = `${lang.name} (${lang.code})`;
    } else {
      this.state.form.odoo_initial_lang = null;
      this.state.langSearch = "";
    }
    this.state.langOpen = false;
  }

  // ── Tags ─────────────────────────────────────────────────────────────

  addTag(tag) {
    if (!this.state.selectedTags.find((t) => t.id === tag.id)) {
      this.state.selectedTags.push(tag);
    }
  }

  removeTag(tagId) {
    const idx = this.state.selectedTags.findIndex((t) => t.id === tagId);
    if (idx !== -1) this.state.selectedTags.splice(idx, 1);
  }

  async createTag(name) {
    const tag = await rpc("/cloud/create_tag", {name, scope: "instance"});
    if (!this.state.allTags.find((t) => t.id === tag.id)) {
      this.state.allTags.push(tag);
    }
    return tag;
  }

  tagBadgeStyle(tag) {
    return tagStyle(tag);
  }
  tagDotStyle(tag) {
    return tagDotStyle(tag);
  }

  /**
   * Label for the expiry pill, or "" when this instance is not expiring.
   *
   * Built here and not in the template so each wording stays one
   * translatable string: split around a ``t-esc`` the extractor emits
   * fragments like "Expires in" and "days", which no translator can
   * place and no language has to order the same way.
   *
   * @returns {string} the pill text, empty when nothing is pending
   */
  get expiryLabel() {
    const days = this.state.inst?.autopurge_days_left;
    if (days === false || days === undefined || days === null) return "";
    if (days === 0) return _t("Expires today");
    if (days === 1) return _t("Expires tomorrow");
    return _t("Expires in %(days)s days", {days});
  }

  /**
   * Start an expiring staging's window over.
   *
   * The one-click answer to the expiry pill. Clears the countdown in
   * place rather than reloading, because the whole point is that
   * keeping an instance costs a single gesture.
   */
  async keep() {
    if (this.state.keeping) return;
    this.state.keeping = true;
    try {
      const res = await rpc("/cloud/keep_instance", {
        instance_id: this.props.instance_id,
      });
      if (res?.ok) {
        this.state.inst.autopurge_days_left = res.days_left;
        this.env.toast?.success(_t("Kept. The expiry window starts over."));
      } else {
        this.env.toast?.error(res?.error || _t("Could not keep this instance."));
      }
    } catch (e) {
      this.env.toast?.error(
        e.data?.message || e.message || _t("Could not keep this instance.")
      );
    } finally {
      this.state.keeping = false;
    }
  }

  // ── Egress whitelist ──────────────────────────────────────────────────

  /**
   * Whether this instance has an egress whitelist to show at all.
   *
   * Staging only, and only once the instance exists: on production
   * this platform does not filter outbound traffic, and on the create
   * form the list is seeded server-side from the chosen host, so there
   * is nothing to edit before saving.
   *
   * @returns {boolean}
   */
  get canShowWhitelist() {
    return !!(
      !this.isCreate &&
      this.state.form.environment !== "production" &&
      this.env.permissions?.can_edit_instance
    );
  }

  /**
   * Add the hostname typed in the input to the pending list.
   *
   * Unlike the host's whitelist this does not save: changing what a
   * staging may reach changes its compose file, so it travels the
   * normal route — save, drift pill, rebuild.
   */
  addWhitelistEntry() {
    const entry = (this.state.newWhitelistEntry || "").trim().toLowerCase();
    if (!entry) return;
    if (!this.state.form.whitelist.includes(entry)) {
      this.state.form.whitelist = [...this.state.form.whitelist, entry];
    }
    this.state.newWhitelistEntry = "";
  }

  /**
   * Drop one hostname from the pending list.
   *
   * @param {string} hostname the entry to remove
   */
  removeWhitelistEntry(hostname) {
    this.state.form.whitelist = this.state.form.whitelist.filter(
      (h) => h !== hostname
    );
  }

  /**
   * Let Enter commit the input, the way every tag field does.
   *
   * @param {KeyboardEvent} ev
   */
  onWhitelistKeydown(ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      this.addWhitelistEntry();
    }
  }

  async save() {
    if (this.state.saving) return;
    const {isValid, firstError} = this.validator.validate(this.state.form);
    if (!isValid) {
      // The new-instance page announces the error once, by taking the
      // focus to the field, which names it; a toast would say it again.
      if (this.isCreate) this.state.focusErrorAsk += 1;
      else this.env.toast?.error(firstError);
      return;
    }
    this.state.saving = true;
    try {
      const vals = {
        ...this.state.form,
        smtp_relay_port: parseInt(this.state.form.smtp_relay_port) || 587,
        odoo_cpus: parseFloat(this.state.form.odoo_cpus) || 0,
        db_cpus: parseFloat(this.state.form.db_cpus) || 0,
        backup_cpus: parseFloat(this.state.form.backup_cpus) || 0,
        smtp_cpus: parseFloat(this.state.form.smtp_cpus) || 0,
        pip_dependencies: this.state.pipDeps,
        apt_dependencies: this.state.aptDeps,
        tag_ids: this.state.selectedTags.map((t) => t.id),
      };
      // Only the secret of the authentication in use travels: what was typed
      // before the other was chosen is hidden, and "" keeps what is stored.
      if (this.isOAuthRelay) {
        vals.smtp_relay_password = "";
      } else {
        vals.smtp_relay_oauth_client_secret = "";
      }
      if (this.isCreate) {
        vals.project_id = this.props.project_id;
        vals.odoo_proxy = "traefik";
        vals.postgres_dbname = "prod";
        const result = await rpc("/cloud/create_instance", {vals});
        if (result && result.blocked) {
          this.env.toast?.error(
            result.message ||
              _t("Cannot create instance: project has unresolved conflicts.")
          );
          return;
        }
        // GitLab repositories the request had no Git reads left for.
        if (result?.warning) {
          this.env.toast?.warning(result.warning);
        }
        // Clear the dirty baseline before navigating so the nav
        // guard doesn't fire on the programmatic transition from
        // the create route to the new instance's detail route.
        this._savedForm = JSON.stringify(this._editedValues());
        // Force-reload the store so the new instance shows up
        // in the sidebar at the destination route.
        await this.env.projectStore?.load(this.props.project_id);
        this.env.navigate("instance_detail", {
          project_id: this.props.project_id,
          instance_id: result.id,
        });
      } else {
        // What was sent is what is saved; an edit made while the save
        // was out stays unsaved.
        const sent = JSON.stringify(this._editedValues());
        const saved = await rpc("/cloud/save_instance", {
          instance_id: this.props.instance_id,
          vals,
        });
        this.state.inst.name = vals.name;
        // The pill "Changes not deployed" follows what was just saved,
        // with no reload: the route says whether the saved configuration
        // now differs from the deployed one.
        if (saved && "config_dirty" in saved) {
          this.state.inst.config_dirty = saved.config_dirty;
        }
        this.env.toast?.success(_t("Changes saved"));
        if (saved?.warning) {
          this.env.toast?.warning(saved.warning);
        }
        this._savedForm = sent;
      }
    } catch (e) {
      // The server's reason only when Odoo refused the save on purpose
      // (a UserError, a ValidationError, an AccessError).
      const refused = e.data?.name?.startsWith?.("odoo.exceptions.");
      this.env.toast?.error(
        (refused && e.data.message) ||
          (this.isCreate
            ? _t("Failed to create instance.")
            : _t("Failed to save instance."))
      );
    } finally {
      this.state.saving = false;
    }
  }

  // ── Repo management ───────────────────────────────────────────────────

  addRepo() {
    this.state.form.repos.unshift({
      id: null,
      url: "",
      branch: "",
      addons: "",
      excludes: "",
      commit_sha: "",
    });
  }

  removeRepo(idx) {
    this.state.form.repos.splice(idx, 1);
  }

  onRepoChange(idx, field, value) {
    const repo = this.state.form.repos[idx];
    repo[field] = value;
    // RepoEditor pins and unpins a saved repository on the server.
    if (field === "commit_sha") this._pinSaved(repo.id, value);

    if (field === "branch" && value && this.state.form.repos[idx].url) {
      this._fetchAndMergeRequirements(this.state.form.repos[idx].url, value);
    }
  }

  async freezeAllRepos() {
    const ids = this.state.form.repos
      .filter((r) => r.id && r.url && r.branch && !r.commit_sha)
      .map((r) => r.id);
    if (!ids.length) return;
    this.state.freezingAll = true;
    try {
      const res = await rpc("/cloud/freeze_all_repos", {
        repo_ids: ids,
        model: "cloud.instance.repo",
      });
      if (res.ok) {
        for (const repo of this.state.form.repos) {
          if (res.results[repo.id]) {
            repo.commit_sha = res.results[repo.id];
            this._pinSaved(repo.id, repo.commit_sha);
          }
        }
      }
    } finally {
      this.state.freezingAll = false;
    }
  }

  async unfreezeAllRepos() {
    const ids = this.state.form.repos
      .filter((r) => r.id && r.commit_sha)
      .map((r) => r.id);
    if (ids.length) {
      await rpc("/cloud/unfreeze_all_repos", {
        repo_ids: ids,
        model: "cloud.instance.repo",
      });
    }
    for (const repo of this.state.form.repos) {
      repo.commit_sha = "";
      this._pinSaved(repo.id, "");
    }
  }

  async _fetchAndMergeRequirements(url, branch) {
    const discards = this._discards;
    try {
      const data = await rpc("/cloud/get_repo_requirements", {url, branch});
      if (!data.ok || !data.found || !data.content) return;
      // Discarded meanwhile: the page is back as saved and stays so.
      if (this._discards !== discards) return;
      const result = mergeRequirements(this.state.pipDeps, data.content, url, branch);
      this.state.pipDeps = result.content;
      if (result.conflicts.length) {
        this.openPipConflictModal(result.conflicts);
      }
    } catch (e) {
      console.warn("Failed to fetch repo requirements:", e);
    }
  }

  goBack() {
    this.env.navigate("project_detail", {project_id: this.props.project_id});
  }

  envLabel(env) {
    return {staging: _t("Staging"), production: _t("Production")}[env] || env;
  }

  // ── Code editor helpers ────────────────────────────────────────────────

  getLineNumbers(field) {
    const text =
      field === "pipDeps"
        ? this.state.pipDeps
        : field === "aptDeps"
        ? this.state.aptDeps
        : this.state.form[field] || "";
    const lines = Math.max(1, text.split("\n").length);
    return Array.from({length: lines}, (_, i) => i + 1);
  }

  onCodeScroll(ev) {
    const gutter = ev.target.previousElementSibling;
    if (gutter && gutter.classList.contains("ic-code-gutter")) {
      gutter.scrollTop = ev.target.scrollTop;
    }
  }

  // ── Instance actions ──────────────────────────────────────────────────

  get statusLabel() {
    const inst = this.state.inst;
    if (!inst) return "";
    if (inst.state === "deploying") return _t("Building...");
    if (inst.state === "deleting") return _t("Removing...");
    if (!inst.deployed) return _t("Not deployed");
    if (inst.running) return _t("Running");
    if (inst.status === "error") return _t("Error");
    // Checked after "error" on purpose: an instance whose stop is
    // expected can still be broken for an unrelated reason, and
    // "Asleep" would hide it.
    if (inst.stop_is_expected) return _t("Asleep");
    return _t("Stopped");
  }

  get statusClass() {
    const inst = this.state.inst;
    if (!inst) return "";
    // Both in-flight lifecycle states reuse the "building" look; the
    // CSS class names predate the state machine and are left alone.
    if (inst.state === "deploying" || inst.state === "deleting") {
      return "st-provisioning";
    }
    if (!inst.deployed) return "st-pending";
    if (inst.running) return "st-running";
    if (inst.status === "error") return "st-error";
    if (inst.stop_is_expected) return "st-asleep";
    return "st-stopped";
  }

  /**
   * Map one container of this instance to its ``rl-sd`` dot modifier.
   *
   * Reads the state the health probe observed for that service. Falls
   * back to the instance-wide ``running`` flag while nothing has been
   * observed yet — a freshly created instance, or one whose first probe
   * has not landed — which is what every dot showed before, so the card
   * degrades to its old behaviour instead of to a wrong one.
   *
   * Only ``odoo`` can be legitimately stopped: a layer that sleeps an
   * instance stops that container and leaves the rest of the stack up.
   *
   * @param {string} svc compose service name, e.g. "odoo" or "db"
   * @returns {string} "up", "asleep" or "down"
   */
  serviceDotClass(svc) {
    const inst = this.state.inst;
    if (!inst) return "down";
    const observed = (inst.service_states || {})[svc];
    if (observed === undefined) return inst.running ? "up" : "down";
    if (observed === "running") return "up";
    if (svc === "odoo" && inst.stop_is_expected) return "asleep";
    return "down";
  }

  // ── Unified job launcher ──────────────────────────────────────────

  /**
   * Central method to enqueue a job and immediately refresh the UI.
   *
   * Says what became of the job — started, or queued and behind what
   * (``announceJob``) — and opens nothing: the job's log opens from the
   * job list, where a queued job waits with its place in line.
   *
   * @param {Function} fn  - async function that enqueues the job and
   *                          returns {ok, job_id, state?, blocked?, error?}
   *                          or a plain job_id (from orm.call).
   * @param {Object} [opts]
   * @param {boolean} [opts.goToOverview=false] - jump to the Overview tab on success
   * @param {string}  [opts.errorLabel]         - fallback toast message
   * @returns {Object|null} the response data, or null on error
   */
  async _enqueueJob(
    fn,
    {
      goToOverview = false,
      errorLabel,
      loadingKey = "actionLoading",
    } = {}
  ) {
    if (this.state[loadingKey]) return null;
    this.state[loadingKey] = true;
    try {
      const data = await fn();
      // orm.call returns a plain id; rpc endpoints return {ok, job_id}
      const jobId = typeof data === "number" ? data : data?.job_id;
      if (data?.ok === false || data?.error) {
        this.env.toast?.error(data.error || errorLabel || _t("Action failed"));
        return null;
      }
      if (data?.blocked) {
        this.state.conflictBlocked = {
          alert_id: data.alert_id,
          alert_code: data.alert_code || "",
          message: data.message,
          conflicts: data.conflicts || [],
        };
      } else {
        if (jobId) {
          await announceJob(this.env.toast, data?.state ? data : jobId);
        }
        if (goToOverview) {
          this.setTab("overview");
        }
      }
      // Immediate refresh so the new job shows in Recent Activity
      this._silentRefresh();
      return data;
    } catch (e) {
      this.env.toast?.error(
        e.data?.message || e.message || errorLabel || _t("Action failed")
      );
      return null;
    } finally {
      this.state[loadingKey] = false;
    }
  }

  async deployInstance() {
    await this._enqueueJob(
      () => rpc("/cloud/deploy_instance", {instance_id: this.props.instance_id}),
      {errorLabel: _t("Deploy failed")}
    );
  }

  async rebuildInstance() {
    await this._enqueueJob(
      () => rpc("/cloud/rebuild_instance", {instance_id: this.props.instance_id}),
      {errorLabel: _t("Rebuild failed")}
    );
  }

  /**
   * Run one of the pluggable instance actions.
   *
   * @param {{code: string, name: string, action_confirm?: string}} action
   *   The action as served in ``inst.custom_actions``.
   */
  async instanceAction(action) {
    const inst = this.state.inst;
    if (!inst?.host_id) return;
    // A disruptive action warns before it runs, not after: the job
    // type carries the notice so an extension can add one without
    // touching this component.
    if (action.action_confirm) {
      const confirmed = await this._confirm({
        title: action.name,
        message: action.action_confirm,
        confirmLabel: _t("Run"),
      });
      if (!confirmed) return;
    }
    await this._enqueueJob(() =>
      this.orm.call("cloud.job", "enqueue", [
        inst.host_id,
        this.props.instance_id,
        action.code,
      ])
    );
  }

  viewLogs() {
    window.open(`/cloud/instance/${this.props.instance_id}/logs`, "_blank");
  }

  async exportInstance() {
    const inst = this.state.inst;
    if (!inst?.host_id) return;
    await this._enqueueJob(() =>
      this.orm.call("cloud.job", "enqueue", [
        inst.host_id,
        this.props.instance_id,
        "export_instance",
      ])
    );
  }

  _confirm(opts) {
      return confirmVia(this.state, opts);
  }

  async deleteInstance() {
    const inst = this.state.inst;
    if (inst.deployed) {
      this.state.removeModal = {instance: inst, typed: ""};
      return;
    }
    const confirmed = await this._confirm({
      title: _t("Delete Instance"),
      message: _t(
        'Permanently delete "%s"? It has never been deployed so no SSH cleanup is needed.'
      ).replace("%s", inst.name),
      confirmLabel: _t("Delete"),
      isDanger: true,
    });
    if (!confirmed) return;
    try {
      await rpc("/cloud/delete_instance", {instance_id: this.props.instance_id});
      this._navigateAfterDelete();
    } catch (e) {
      this.env.toast?.error(e.data?.message || e.message || _t("Delete failed"));
    }
  }

  closeRemoveModal() {
    this.state.removeModal = null;
  }

  /**
   * Whether what the operator typed unlocks "Delete completely".
   *
   * Deleting now takes the backups with it, so there is no way back
   * from this button and it asks for the instance name. The name — not
   * a fixed word like DELETE — because the expensive mistake here is
   * almost never "I did not mean to delete": it is "I deleted the wrong
   * one", two similar instances, wrong tab. Typing the name catches
   * that; a fixed word catches nothing.
   *
   * Exact match on purpose: no trimming, no case folding. A barrier
   * that forgives is a barrier you clear on autopilot.
   *
   * @returns {boolean} true when the typed text unlocks the button.
   */
  get removeConfirmed() {
    const modal = this.state.removeModal;
    return !!modal && modal.typed === modal.instance.name;
  }

  /** Remove a deployed instance from the host; keepInPanel decides
   * whether the record survives as a re-deployable draft or is
   * unlinked once the host teardown succeeds. */
  async _removeFromHost(keepInPanel) {
    const inst = this.state.inst;
    this.state.removeModal = null;
    const data = await this._enqueueJob(() =>
      this.orm.call("cloud.job", "enqueue", [
        inst.host_id,
        this.props.instance_id,
        "delete_instance",
        {keep_in_panel: keepInPanel},
      ])
    );
    if (data) this._waitForDeleteJob(data);
  }

  _waitForDeleteJob(jobId) {
    const poll = async () => {
      if (!this._safePoll.alive) return;
      try {
        const [job] = await this.orm.call("cloud.job", "load_jobs", [jobId]);
        if (!this._safePoll.alive) return;
        if (job && ["done", "failed"].includes(job.state)) {
          this._navigateAfterDelete();
          return;
        }
        // Cancelled — in the queue, or by its own executor: the instance
        // stays, so the page does too.
        if (job?.state === "cancelled") {
          this._silentRefresh();
          return;
        }
        this._safePoll.schedule(poll, 3000);
      } catch (_) {
        this._safePoll.schedule(poll, 3000);
      }
    };
    this._safePoll.schedule(poll, 2000);
  }

  async _navigateAfterDelete() {
    // Reload so the deleted row is gone from the store before the
    // destination route reads it. The store's bus subscription
    // would catch it eventually, but the user clicks "delete" and
    // navigates immediately — we can't race the debouncer.
    await this.env.projectStore?.load(this.props.project_id);
    const next = this.env.projectStore?.getNextInstance(this.props.instance_id);
    if (next) {
      this.env.navigate("instance_detail", {
        project_id: this.props.project_id,
        instance_id: next.id,
      });
    } else {
      this.env.navigate("project_detail", {project_id: this.props.project_id});
    }
  }
}
