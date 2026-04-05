import { Component, useState, onWillStart, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { PasswordInput } from "../password_input/password_input";

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
    static components = { PasswordInput };

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

        if (!this.isNew) {
            onWillStart(() => this.loadBackend());
        }
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

    _validate() {
        const f = this.state.form;
        const missing = [];
        if (!f.name?.trim()) missing.push(_t("Name"));
        if (!f.s3_bucket?.trim()) missing.push(_t("Bucket"));
        return missing;
    }

    async save() {
        if (this.state.saving) return;
        const missing = this._validate();
        if (missing.length) {
            this.env.toast?.error(_t("Required fields missing: %s").replace('%s', missing.join(", ")));
            return;
        }
        this.state.saving = true;
        try {
            const vals = { ...this.state.form };
            if (this.isNew) {
                const result = await rpc("/cloud/create_backup_backend", { vals });
                this.env.navigate("backup_backend_detail", { backend_id: result.id });
            } else {
                await rpc("/cloud/save_backup_backend", {
                    backend_id: this.props.backend_id,
                    vals,
                });
                this.state.backend.name = vals.name;
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
