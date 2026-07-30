/** @odoo-module **/

import {_t} from "@web/core/l10n/translation";

/**
 * Restore from an uploaded dump (upload or rsync).
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const RestoreUploadMixin = (Base) =>
  class extends Base {
    // ── Restore DB ───────────────────────────────────────────────────────

    openRestore() {
      this.state.restoreDialog = {
        inst: this.state.inst,
        mode: "browser",
        file: null,
        uploading: false,
        copied: false,
        error: null,
        // Non-production only: production always takes a safety
        // backup first, no opt-out, so the checkbox is hidden there.
        backupBeforeRestore: false,
      };
    }

    closeRestore() {
      this.state.restoreDialog = null;
    }

    onRestoreFileChange(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      const d = this.state.restoreDialog;
      d.file = file;
      d.error = null;
      const TWO_GB = 2 * 1024 * 1024 * 1024;
      if (file.size > TWO_GB) {
        d.mode = "rsync";
        d.error = _t("File exceeds 2 GB — switched to SSH / rsync mode.");
      }
    }

    async copyRsyncCmd() {
      const d = this.state.restoreDialog;
      await navigator.clipboard.writeText(this.rsyncCommand());
      d.copied = true;
      setTimeout(() => {
        if (d) d.copied = false;
      }, 2000);
    }

    rsyncCommand() {
      const inst = this.state.inst;
      const user = inst.host_user || "ubuntu";
      const ip = inst.host_ip || inst.host;
      const port = inst.host_port || 22;
      const dest = `/tmp/incubacloud-restore-${this.props.instance_id}.zip`;
      const portFlag = port !== 22 ? ` -e "ssh -p ${port}"` : "";
      return `rsync -P --inplace${portFlag} <local_backup.zip> ${user}@${ip}:${dest}`;
    }

    async doRestore() {
      const d = this.state.restoreDialog;
      if (!d || d.uploading) return;
      d.error = null;
      d.uploading = true;
      try {
        if (d.mode === "browser") {
          if (!d.file) {
            d.error = _t("Please select a backup file first.");
            d.uploading = false;
            return;
          }
          const fd = new FormData();
          fd.append("backup_file", d.file);
          fd.append("csrf_token", odoo.csrf_token);
          fd.append("backup_before_restore", d.backupBeforeRestore ? "true" : "false");
          const resp = await fetch(`/cloud/instance/${this.props.instance_id}/restore`, {
            method: "POST",
            body: fd,
          });
          if (resp.status === 413) {
            throw new Error(
              _t(
                "File too large for browser upload. Please use the SSH / rsync method instead."
              )
            );
          }
          if (!resp.ok) {
            let msg = `Server error (${resp.status})`;
            try {
              const r = await resp.json();
              if (r.error) msg = r.error;
            } catch {}
            throw new Error(msg);
          }
          // A 200 with a non-JSON body (typical signal of a
          // session timeout that returned the login page) would
          // otherwise blow up inside resp.json() with a parser
          // error that surfaced as a confusing toast. Detect it
          // here and prompt the user to log in again.
          const ctype = (resp.headers.get("Content-Type") || "").toLowerCase();
          if (!ctype.includes("application/json")) {
            throw new Error(_t("Session expired. Please reload and try again."));
          }
          const result = await resp.json();
          if (result.error) throw new Error(result.error);
          if (result.job_id) window.open(`/cloud/log/${result.job_id}`, "_blank");
        } else {
          const remote = `/tmp/incubacloud-restore-${this.props.instance_id}.zip`;
          const jobId = await this.orm.call("cloud.instance", "restore_db", [
            [this.props.instance_id],
            {
              mode: "rsync",
              remote_path: remote,
              backup_before_restore: d.backupBeforeRestore,
            },
          ]);
          if (jobId) window.open(`/cloud/log/${jobId}`, "_blank");
        }
        this.state.restoreDialog = null;
      } catch (e) {
        d.error =
          e.data?.message || e.message || _t("Operation failed. Please try again.");
      } finally {
        if (d) d.uploading = false;
      }
    }

    // ── Connect as user ──────────────────────────────────────────────────

    /**
     * Whether the current user may open the connect-as dialog at all.
     * Production requires Developer+; staging keeps the Stakeholder floor.
     * The backend enforces the same rule — this only keeps the UI honest.
     *
     * @returns {boolean}
     */
  };
