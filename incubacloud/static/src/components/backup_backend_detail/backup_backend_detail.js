import { Component, useState, onWillStart, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { PasswordInput } from "../password_input/password_input";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
import { RlSelect } from "../rl_select/rl_select";
import { useNavGuard } from "../../utils/use_nav_guard";
import { useFormValidation } from "../../utils/use_form_validation";
import { required, email, retention } from "../../utils/validators";

const EMPTY_FORM = () => ({
    name:                  "",
    backend_type:          "s3",
    s3_bucket:             "",
    s3_path:               "backups",
    s3_endpoint_url:       "",
    s3_access_key_id:      "",
    s3_secret_access_key:  "",
    passphrase:            "",
    backup_image_version:  "latest",
    email_from:            "",
    email_to:              "",
    smtp_report_success:   true,
    backup_retention:      "3M",
    deletion_via_cron:     false,
    backup_tz:             "UTC",
    quota_gb:              0,
    alert_threshold_pct:   0,
    alert_email:           "",
});

export class BackupBackendDetail extends Component {
    static props = { backend_id: { type: Number, optional: true } };
    static template = "incubacloud.BackupBackendDetail";
    static components = { PasswordInput, IcConfirmDialog, RlSelect };

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
            backend: this.isNew ? { name: _t("New Backup Backend") } : null,
            form: this.isNew ? EMPTY_FORM() : {},
            has_s3_secret_access_key: false,
            has_passphrase: false,
            // Usage display (separate from form because not editable here)
            last_measured_gb: 0,
            last_measured_at: "",
            confirmDialog: null,
        });

        this._savedForm = null;

        this.validator = useFormValidation(() => ({
            name: [required(_t("Backend name is required"))],
            s3_bucket: [required(_t("Bucket is required"))],
            email_from: [email(_t("Sender email is invalid"))],
            email_to: [email(_t("Recipient email is invalid"))],
            backup_retention: [retention(_t("Use N + D/M/Y (e.g. 30D, 3M, 1Y)"))],
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
            (opts) => this._confirm(opts),
        );
    }

    get hasUnsavedChanges() {
        if (!this._savedForm) return false;
        return JSON.stringify(this.state.form) !== this._savedForm;
    }

    _snapshotForm() {
        this._savedForm = JSON.stringify(this.state.form);
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
            this.state.last_measured_gb = b.last_measured_gb || 0;
            this.state.last_measured_at = b.last_measured_at || "";
            this.state.form = {
                name:                 b.name || "",
                backend_type:         b.backend_type || "s3",
                s3_bucket:            b.s3_bucket || "",
                s3_path:              b.s3_path || "backups",
                s3_endpoint_url:      b.s3_endpoint_url || "",
                s3_access_key_id:     b.s3_access_key_id || "",
                s3_secret_access_key: "",
                passphrase:           "",
                backup_image_version: b.backup_image_version || "latest",
                email_from:           b.email_from || "",
                email_to:             b.email_to || "",
                smtp_report_success:  b.smtp_report_success !== false,
                backup_retention:     b.backup_retention || "3M",
                deletion_via_cron:    b.deletion_via_cron || false,
                backup_tz:            b.backup_tz || "UTC",
                quota_gb:             b.quota_gb || 0,
                alert_threshold_pct:  b.alert_threshold_pct || 0,
                alert_email:          b.alert_email || "",
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
        if (own === -1) threshold = 101;          // never warn
        else if (own > 0) threshold = own;
        else threshold = 80;                       // inherit fallback
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
     * (Notifications "Report on success", Advanced "Deletion cron"), which
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

    async save() {
        if (this.state.saving) return;
        const { isValid, firstError } = this.validator.validate(this.state.form);
        if (!isValid) {
            this.env.toast?.error(firstError);
            return;
        }
        this.state.saving = true;
        try {
            const vals = { ...this.state.form };
            if (this.isNew) {
                const result = await rpc("/cloud/create_backup_backend", { vals });
                this._snapshotForm();
                this.env.navigate("backup_backend_detail", { backend_id: result.id });
            } else {
                await rpc("/cloud/save_backup_backend", {
                    backend_id: this.props.backend_id,
                    vals,
                });
                this.state.backend.name = vals.name;
                this._snapshotForm();
                this.env.toast?.success(_t("Changes saved"));
            }
        } catch (e) {
            this.env.toast?.error(this.isNew
                ? _t("Failed to create backend.")
                : _t("Failed to save backend."));
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
            if (result.ok) {
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

    _confirm({ title, message, confirmLabel = _t("Confirm"), isDanger = false }) {
        return new Promise((resolve) => {
            this.state.confirmDialog = {
                title, message, confirmLabel, isDanger,
                onConfirm: () => { this.state.confirmDialog = null; resolve(true); },
                onCancel:  () => { this.state.confirmDialog = null; resolve(false); },
            };
        });
    }

    async deleteBackend() {
        const confirmed = await this._confirm({
            title: _t("Delete Backup Backend"),
            message: _t("This will permanently delete this backup backend. It can only be deleted if no instances are using it."),
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
        if (!f.s3_bucket) return "";
        const path = (f.s3_path || "").replace(/^\/|\/$/g, "");
        return path
            ? `boto3+s3://${f.s3_bucket}/${path}`
            : `boto3+s3://${f.s3_bucket}`;
    }
}
