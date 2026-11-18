import {Component, useState, onWillStart, useEnv} from "@odoo/owl";
import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";
import {PasswordInput} from "../password_input/password_input";
import {IcConfirmDialog} from "../ic_confirm_dialog/ic_confirm_dialog";
import {RlSelect} from "../rl_select/rl_select";
import {restoreForm, SaveBar, unsavedCount} from "../settings_form/save_bar";
import {SettingAdvanced} from "../settings_form/settings_form";
import {useNavGuard} from "../../utils/use_nav_guard";
import {useFormValidation} from "../../utils/use_form_validation";
import {required, email, retention, hostname, portRange, when} from "../../utils/validators";
import {confirmVia} from "../../utils/use_confirm";

const EMPTY_FORM = () => ({
  name: "",
  backend_type: "s3",
  s3_bucket: "",
  s3_path: "backups",
  s3_endpoint_url: "",
  s3_region: "",
  s3_access_key_id: "",
  s3_secret_access_key: "",
  sftp_host: "",
  sftp_port: 22,
  sftp_user: "",
  sftp_path: "backups",
  sftp_auth_method: "key",
  sftp_password: "",
  passphrase: "",
  backup_image_version: "latest",
  email_from: "",
  email_to: "",
  smtp_report_success: true,
  backup_retention: "3M",
  retention_owner: "cron",
  backup_tz: "UTC",
  quota_gb: 0,
  alert_threshold_pct: 0,
  alert_email: "",
});

// Folded under Advanced at the end of General: their defaults serve nearly
// every destination (form standard §4). The template hands the same list to
// the fold, through ``advancedFields``.
const ADVANCED_FIELDS = ["backup_retention", "backup_tz", "retention_owner"];

/** @returns {boolean} whether the form is an S3 destination's */
const isS3 = (form) => (form.backend_type || "s3") === "s3";
/** @returns {boolean} whether the form is an SFTP destination's */
const isSftp = (form) => form.backend_type === "sftp";
/** @returns {boolean} whether the form is an SFTP destination's that logs in with a password */
const isSftpPassword = (form) => isSftp(form) && form.sftp_auth_method === "password";

export class BackupBackendDetail extends Component {
  static props = {backend_id: {type: Number, optional: true}};
  static template = "incubacloud.BackupBackendDetail";
  static components = {PasswordInput, IcConfirmDialog, RlSelect, SaveBar, SettingAdvanced};

  setup() {
    this.env = useEnv();
    this.isNew = !this.props.backend_id;

    this.state = useState({
      tab: "general",
      loading: !this.isNew,
      saving: false,
      deleting: false,
      testing: false,
      refreshingUsage: false,
      error: null,
      backend: this.isNew ? {name: _t("New Backup Backend")} : null,
      form: this.isNew ? EMPTY_FORM() : {},
      has_s3_secret_access_key: false,
      has_passphrase: false,
      has_sftp_password: false,
      retention_owner_declared: true,
      // Usage display (separate from form because not editable here)
      last_measured_gb: 0,
      last_measured_at: "",
      confirmDialog: null,
      // SFTP: the public key, the trusted server key, and the one the
      // server presents while the person compares it.
      sftp: {public_key: "", fingerprint: "", trusted_at: "", trusted_by: ""},
      presented: null,
      readingKey: false,
      trustingKey: false,
    });

    this._savedForm = null;

    this.validator = useFormValidation(() => ({
      name: [required(_t("Backend name is required"))],
      s3_bucket: [when(isS3, required(_t("Bucket is required")))],
      sftp_host: [
        when(isSftp, required(_t("Server is required"))),
        when(isSftp, hostname(_t("Enter a host name or an IPv4 address"))),
      ],
      sftp_port: [when(isSftp, portRange(_t("Use a port between 1 and 65535.")))],
      sftp_user: [when(isSftp, required(_t("User is required")))],
      sftp_password: [
        when(
          (form) => isSftpPassword(form) && !this.state.has_sftp_password,
          required(_t("Password is required"))
        ),
      ],
      email_from: [email(_t("Sender email is invalid"))],
      email_to: [email(_t("Recipient email is invalid"))],
      backup_retention: [
        required(_t("Retention period is required")),
        retention(_t("Use N + D/W/M/Y (e.g. 30D, 2W, 3M, 1Y)")),
      ],
    }));

    if (!this.isNew) {
      onWillStart(() => this.loadBackend());
    } else {
      // For the "new" flow the form starts with defaults — treat
      // those as the baseline so the nav guard only fires if the
      // user actually typed something.
      this._snapshotForm();
    }

    useNavGuard(
      () => this.hasUnsavedChanges,
      (opts) => this._confirm(opts)
    );
  }

  get hasUnsavedChanges() {
    if (!this._savedForm) return false;
    return JSON.stringify(this.state.form) !== this._savedForm;
  }

  _snapshotForm() {
    this._savedForm = JSON.stringify(this.state.form);
  }

  /** @returns {number} how many fields of the form are unsaved */
  get unsavedCount() {
    return this._savedForm ? unsavedCount(this.state.form, JSON.parse(this._savedForm)) : 0;
  }

  /** @returns {string[]} the fields folded under Advanced, for the fold to count */
  get advancedFields() {
    return ADVANCED_FIELDS;
  }

  /** @returns {number} how many of the fields folded under Advanced are unsaved */
  get advancedChanged() {
    if (!this._savedForm) return 0;
    const saved = JSON.parse(this._savedForm);
    const pick = (form) => Object.fromEntries(ADVANCED_FIELDS.map((key) => [key, form[key]]));
    return unsavedCount(pick(this.state.form), pick(saved));
  }

  /**
   * Whether Advanced stays open: a retention period to fix, or a retention
   * owner still to confirm, is inside it.
   *
   * @returns {boolean}
   */
  get advancedNeedsLook() {
    return Boolean(this.validator.fieldError("backup_retention"))
      || (!this.isNew && !this.state.retention_owner_declared);
  }

  /** Put the form back as it was loaded or last saved. */
  discard() {
    if (!this._savedForm) return;
    restoreForm(this.state.form, JSON.parse(this._savedForm));
    this.validator.clearErrors();
  }

  async loadBackend() {
    this.state.loading = true;
    try {
      const b = await rpc("/cloud/get_backup_backend", {
        backend_id: this.props.backend_id,
      });
      if (b.error) {
        this.state.error = b.error;
        return;
      }
      this.state.backend = b;
      this.state.has_s3_secret_access_key = b.has_s3_secret_access_key || false;
      this.state.has_passphrase = b.has_passphrase || false;
      this.state.has_sftp_password = b.has_sftp_password || false;
      this.state.last_measured_gb = b.last_measured_gb || 0;
      this.state.last_measured_at = b.last_measured_at || "";
      this.state.retention_owner_declared = b.retention_owner_declared !== false;
      this._setSftp(b);
      this.state.form = {
        name: b.name || "",
        backend_type: b.backend_type || "s3",
        s3_bucket: b.s3_bucket || "",
        s3_path: b.s3_path || "backups",
        s3_endpoint_url: b.s3_endpoint_url || "",
        s3_region: b.s3_region || "",
        s3_access_key_id: b.s3_access_key_id || "",
        s3_secret_access_key: "",
        sftp_host: b.sftp_host || "",
        sftp_port: b.sftp_port || 22,
        sftp_user: b.sftp_user || "",
        sftp_path: b.sftp_path || "",
        sftp_auth_method: b.sftp_auth_method || "key",
        sftp_password: "",
        passphrase: "",
        backup_image_version: b.backup_image_version || "latest",
        email_from: b.email_from || "",
        email_to: b.email_to || "",
        smtp_report_success: b.smtp_report_success !== false,
        backup_retention: b.backup_retention || "3M",
        retention_owner: b.retention_owner || "cron",
        backup_tz: b.backup_tz || "UTC",
        quota_gb: b.quota_gb || 0,
        alert_threshold_pct: b.alert_threshold_pct || 0,
        alert_email: b.alert_email || "",
      };
      this._snapshotForm();
    } catch (e) {
      this.state.error = _t("Failed to load backup backend.");
    } finally {
      this.state.loading = false;
    }
  }

  async refreshUsage() {
    if (this.state.refreshingUsage) return;
    this.state.refreshingUsage = true;
    try {
      const result = await rpc("/cloud/measure_backup_usage", {
        backend_id: this.props.backend_id,
      });
      if (result.ok) {
        this.state.last_measured_gb = result.last_measured_gb || 0;
        this.state.last_measured_at = result.last_measured_at || "";
        this.env.toast?.success(_t("Usage refreshed."));
      } else {
        this.env.toast?.error(result.error || _t("Failed to measure usage."));
      }
    } catch (e) {
      this.env.toast?.error(_t("Unexpected error measuring usage."));
    } finally {
      this.state.refreshingUsage = false;
    }
  }

  get usagePct() {
    const q = parseFloat(this.state.form.quota_gb) || 0;
    if (q <= 0) return null;
    return Math.min(100, (this.state.last_measured_gb / q) * 100);
  }

  get usageBarClass() {
    const pct = this.usagePct;
    if (pct === null) return "";
    // Resolve effective threshold the same way the backend does so
    // the bar colour matches the alert behaviour. We don't have the
    // settings default here; default 80 mirrors the system default.
    const own = parseInt(this.state.form.alert_threshold_pct) || 0;
    let threshold;
    if (own === -1) threshold = 101; // never warn
    else if (own > 0) threshold = own;
    else threshold = 80; // inherit fallback
    if (pct >= threshold) return "danger";
    if (pct >= threshold * 0.75) return "warning";
    return "ok";
  }

  /**
   * Relay ``rl-gauge-fill`` band class for the Usage gauge.
   *
   * Maps the resolved alert band (``usageBarClass``: ""/"warning"/
   * "danger") onto the bare modifiers the rl-gauge primitive uses
   * (""/"warn"/"crit"), so the gauge colour still tracks the alert
   * threshold exactly as before.
   *
   * @returns {string} "" | "warn" | "crit"
   */
  get gaugeBand() {
    const band = this.usageBarClass;
    if (band === "danger") return "crit";
    if (band === "warning") return "warn";
    return "";
  }

  onInput(field, ev) {
    this.state.form[field] = ev.target.value;
  }

  onCheckbox(field, ev) {
    this.state.form[field] = ev.target.checked;
  }

  /**
   * Flip a boolean form field. Backs the Relay ``rl-sw`` switch buttons
   * (Notifications "Report on success"), which
   * are ``<button>`` elements with no ``checked`` property — so they
   * can't reuse ``onCheckbox`` directly.
   *
   * @param {string} field form key to toggle
   */
  toggleField(field) {
    this.state.form[field] = !this.state.form[field];
  }

  onQuotaInput(ev) {
    this.state.form.quota_gb = parseFloat(ev.target.value) || 0;
  }

  onThresholdInput(ev) {
    this.state.form.alert_threshold_pct = parseInt(ev.target.value, 10) || 0;
  }

  onPasswordChange(field, value) {
    this.state.form[field] = value;
  }

  setTab(tab) {
    this.state.tab = tab;
  }

  /**
   * Keep what the server says of an SFTP destination's keys.
   *
   * @param {object} b the destination as a route returns it
   */
  _setSftp(b) {
    this.state.sftp = {
      public_key: b.sftp_public_key || "",
      fingerprint: b.sftp_host_key_fingerprint || "",
      trusted_at: b.sftp_host_key_trusted_at || "",
      trusted_by: b.sftp_host_key_trusted_by || "",
    };
    this.state.presented = null;
  }

  /** @returns {boolean} whether the form is an SFTP destination's */
  get isSftp() {
    return isSftp(this.state.form);
  }

  /** @returns {boolean} whether the form is an SFTP destination's that logs in with a password */
  get isSftpPassword() {
    return isSftpPassword(this.state.form);
  }

  /** @returns {Array} the ways an SFTP destination can log in */
  get authOptions() {
    return [
      {value: "key", label: _t("Key (recommended)")},
      {value: "password", label: _t("Password")},
    ];
  }

  /** @returns {Array} the retention owners this type of destination has */
  get retentionOwnerOptions() {
    const options = [
      {value: "cron", label: _t("The backup service (Duplicity)")},
      {value: "lifecycle", label: _t("A lifecycle rule on the bucket")},
      {value: "none", label: _t("Nobody: keep everything")},
    ];
    return this.isSftp ? options.filter((o) => o.value !== "lifecycle") : options;
  }

  /**
   * Pick the type of a new destination; a saved one keeps its type.
   *
   * @param {string} type "s3" or "sftp"
   */
  setType(type) {
    if (!this.isNew) return;
    this.state.form.backend_type = type;
    if (type === "sftp" && this.state.form.retention_owner === "lifecycle") {
      this.state.form.retention_owner = "cron";
    }
  }

  /** @returns {Array} the types a destination can have */
  get typeOptions() {
    return [
      {value: "s3", label: _t("S3 Compatible")},
      {value: "sftp", label: _t("SFTP")},
    ];
  }

  /** @returns {boolean} whether the type is fixed: once created, or managed */
  get typeLocked() {
    return !this.isNew || Boolean(this.state.backend?.managed_reservation_id);
  }

  /** @returns {string} the line for the server's authorized_keys */
  get authorizedKeysLine() {
    return this.state.sftp.public_key ? `restrict ${this.state.sftp.public_key}` : "";
  }

  /**
   * Whether this save moves an SFTP destination with a trusted key to
   * another server or port, which drops that key.
   *
   * @returns {boolean}
   */
  get _movesTrustedEndpoint() {
    if (this.isNew || !this.isSftp || !this.state.sftp?.fingerprint || !this._savedForm) {
      return false;
    }
    const saved = JSON.parse(this._savedForm);
    return saved.sftp_host !== this.state.form.sftp_host
      || Number(saved.sftp_port) !== Number(this.state.form.sftp_port);
  }

  /**
   * Whether this save switches a saved SFTP destination between a key and a
   * password, which drops the other secret.
   *
   * @returns {boolean}
   */
  get _switchesLogin() {
    if (this.isNew || !this.isSftp || !this._savedForm) return false;
    return JSON.parse(this._savedForm).sftp_auth_method !== this.state.form.sftp_auth_method;
  }

  /** Copy *text* to the clipboard and say so. */
  async copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.env.toast?.success(_t("Copied."));
    } catch {
      this.env.toast?.error(_t("Could not copy: select the text and copy it."));
    }
  }

  /** Read the key the SFTP server presents, trusting nothing. */
  async readHostKey() {
    if (this.state.readingKey) return;
    this.state.readingKey = true;
    try {
      const result = await rpc("/cloud/sftp_backend_read_host_key", {
        backend_id: this.props.backend_id,
      });
      if (result.ok) {
        this.state.presented = {fingerprint: result.fingerprint, key_type: result.key_type};
      } else {
        this.env.toast?.error(result.error || _t("Could not read the server's key."));
      }
    } catch {
      this.env.toast?.error(_t("Could not read the server's key."));
    } finally {
      this.state.readingKey = false;
    }
  }

  /** Trust the key the person just compared, if the server still presents it. */
  async trustHostKey() {
    if (this.state.trustingKey || !this.state.presented) return;
    this.state.trustingKey = true;
    try {
      const result = await rpc("/cloud/sftp_backend_trust_host_key", {
        backend_id: this.props.backend_id,
        expected_fingerprint: this.state.presented.fingerprint,
      });
      if (result.ok) {
        this.state.sftp.fingerprint = result.sftp_host_key_fingerprint;
        this.state.sftp.trusted_at = result.sftp_host_key_trusted_at || "";
        this.state.sftp.trusted_by = result.sftp_host_key_trusted_by || "";
        this.state.presented = null;
        this.env.toast?.success(_t("Server key trusted."));
      } else {
        this.state.presented = null;
        this.env.toast?.error(result.error || _t("The key was not trusted."));
      }
    } catch {
      this.env.toast?.error(_t("The key was not trusted."));
    } finally {
      this.state.trustingKey = false;
    }
  }

  /** Replace the destination's key pair, once confirmed. */
  async regenerateKey() {
    const confirmed = await this._confirm({
      title: _t("Regenerate the SFTP key"),
      message: _t(
        "A new key pair replaces this one. Add the new public key to the server's authorized_keys and keep the old line until every instance on this destination has been rebuilt: until then they log in with the old key."
      ),
      confirmLabel: _t("Regenerate"),
      isDanger: true,
    });
    if (!confirmed) return;
    try {
      const result = await rpc("/cloud/sftp_backend_regenerate_key", {
        backend_id: this.props.backend_id,
      });
      if (result.ok) {
        this.state.sftp.public_key = result.sftp_public_key;
        this.env.toast?.success(_t("New key generated: add it to the server."));
      } else {
        this.env.toast?.error(result.error || _t("Could not regenerate the key."));
      }
    } catch {
      this.env.toast?.error(_t("Could not regenerate the key."));
    }
  }

  async save() {
    if (this.state.saving) return;
    const {isValid, firstError} = this.validator.validate(this.state.form);
    if (!isValid) {
      this.env.toast?.error(firstError);
      return;
    }
    if (this._movesTrustedEndpoint) {
      const confirmed = await this._confirm({
        title: _t("Change the SFTP server"),
        message: _t(
          "Another server or port drops the trusted server key. Read and trust the new one before the next deploy or rebuild of the instances on this destination: until then they are refused."
        ),
        confirmLabel: _t("Change"),
      });
      if (!confirmed) return;
    }
    if (this._switchesLogin) {
      const toPassword = this.isSftpPassword;
      const confirmed = await this._confirm({
        title: _t("Change how the panel logs in"),
        message: toPassword
          ? _t(
              "The key pair is deleted and the backups log in with the password. Rebuild the instances on this destination: until then they log in with the old key, so keep its line in authorized_keys until every one has been rebuilt."
            )
          : _t(
              "The password is deleted and the panel generates a new key pair. Add its public key to the server's authorized_keys, then rebuild the instances on this destination: until then they log in with the password."
            ),
        confirmLabel: _t("Change"),
      });
      if (!confirmed) return;
    }
    this.state.saving = true;
    try {
      const vals = {...this.state.form};
      if (this.isNew) {
        const result = await rpc("/cloud/create_backup_backend", {vals});
        this.env.refreshNavCounts?.();
        this._snapshotForm();
        this.env.navigate("backup_backend_detail", {backend_id: result.id});
      } else {
        // What was sent is what is saved; an edit made while the save
        // was out stays unsaved.
        const sent = JSON.stringify(vals);
        const result = await rpc("/cloud/save_backup_backend", {
          backend_id: this.props.backend_id,
          vals,
        });
        this.state.backend.name = vals.name;
        // Saving the retention owner confirms it: the note and the open
        // fold go with the answer, not with the next load.
        if (result?.retention_owner_declared !== undefined) {
          this.state.retention_owner_declared = result.retention_owner_declared;
        }
        this._savedForm = sent;
        if (this.isSftp) {
          // The server drops the trusted key with a new endpoint,
          // recomputes the path, and makes or drops the key pair with
          // another way of logging in.
          await this.loadBackend();
        }
        this.env.toast?.success(_t("Changes saved"));
      }
    } catch (e) {
      this.env.toast?.error(
        this.isNew ? _t("Failed to create backend.") : _t("Failed to save backend.")
      );
    } finally {
      this.state.saving = false;
    }
  }

  async testConnection() {
    if (this.state.testing) return;
    this.state.testing = true;
    try {
      const result = await rpc("/cloud/test_backup_backend", {
        backend_id: this.props.backend_id,
      });
      if (result.ok && this.isSftp) {
        const free = result.free_bytes;
        this.env.toast?.success(
          free === null || free === undefined
            ? _t("Connection successful — the folder is writable.")
            : _t("Connection successful — the folder is writable, %(free)s GB free.", {
                free: (free / 1024 ** 3).toFixed(1),
              })
        );
      } else if (result.ok) {
        this.env.toast?.success(_t("Connection successful — bucket is accessible."));
      } else {
        this.env.toast?.error(result.error || _t("Connection failed."));
      }
    } catch (e) {
      this.env.toast?.error(_t("Unexpected error during test."));
    } finally {
      this.state.testing = false;
    }
  }

  /** Open the shared confirmation dialog. See utils/use_confirm.js. */
  _confirm(opts) {
      return confirmVia(this.state, opts);
  }

  async deleteBackend() {
    const confirmed = await this._confirm({
      title: _t("Delete Backup Backend"),
      message: _t(
        "This will permanently delete this backup backend. It can only be deleted once nothing points at it — no instance (archived ones included), no project default, and not the global default. The backups already stored are not deleted, but they stop being reachable from the panel."
      ),
      confirmLabel: _t("Delete"),
      isDanger: true,
    });
    if (!confirmed) return;
    this.state.deleting = true;
    try {
      const result = await rpc("/cloud/delete_backup_backend", {
        backend_id: this.props.backend_id,
      });
      if (result.error) {
        this.env.toast?.error(result.error);
      } else {
        this.env.refreshNavCounts?.();
        this.env.navigate("backup_backends");
      }
    } catch {
      this.env.toast?.error(_t("Failed to delete backend."));
    } finally {
      this.state.deleting = false;
    }
  }

  goBack() {
    this.env.navigate("backup_backends");
  }

  get backupDst() {
    const f = this.state.form;
    if (isSftp(f)) {
      // The server's own reading of the path, as saved.
      return this.state.backend?.backup_dst || "";
    }
    if (!f.s3_bucket) return "";
    const path = (f.s3_path || "").replace(/^\/|\/$/g, "");
    return path ? `boto3+s3://${f.s3_bucket}/${path}` : `boto3+s3://${f.s3_bucket}`;
  }
}
