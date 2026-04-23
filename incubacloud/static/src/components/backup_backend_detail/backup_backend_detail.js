import { Component, useState, onWillStart, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { PasswordInput } from "../password_input/password_input";
import { IcConfirmDialog } from "../ic_confirm_dialog/ic_confirm_dialog";
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
});

export class BackupBackendDetail extends Component {
    static props = { backend_id: { type: Number, optional: true } };
    static template = "incubacloud.BackupBackendDetail";
    static components = { PasswordInput, IcConfirmDialog };

    setup() {
        this.env = useEnv();
        this.isNew = !this.props.backend_id;

        this.state = useState({
            tab: "general",
            loading: !this.isNew,
            saving: false,
            deleting: false,
            testing: false,
            error: null,
            backend: this.isNew ? { name: _t("New Backup Backend") } : null,
            form: this.isNew ? EMPTY_FORM() : {},
            has_s3_secret_access_key: false,
            has_passphrase: false,
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
            };
            this._snapshotForm();
        } catch (e) {
            this.state.error = _t("Failed to load backup backend.");
        } finally {
            this.state.loading = false;
        }
    }

    onInput(field, ev) {
        this.state.form[field] = ev.target.value;

    }

    onCheckbox(field, ev) {
        this.state.form[field] = ev.target.checked;

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
