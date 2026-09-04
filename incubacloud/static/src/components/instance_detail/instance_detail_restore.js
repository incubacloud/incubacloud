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
        progress: 0,
        copied: false,
        error: null,
        // SSH mode: the grant the panel opened, the private key it
        // handed us (held in memory only, never sent anywhere), and
        // what the host reports having received.
        granting: false,
        grant: null,
        privateKey: "",
        received: null,
        url: "",
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
      d.progress = 0;
      // The browser upload sends the archive in pieces, so no proxy in
      // front of the panel caps it any more. What remains is the disk
      // the panel stages it on, which the server enforces and reports.
      const TWO_GB = 2 * 1024 * 1024 * 1024;
      if (file.size > TWO_GB) {
        d.mode = "ssh";
        d.error = _t("File exceeds 2 GB — switched to SSH upload.");
      }
    }

    async copyUploadCmd() {
      const d = this.state.restoreDialog;
      await navigator.clipboard.writeText(this.uploadCommand());
      d.copied = true;
      setTimeout(() => {
        if (d) d.copied = false;
      }, 2000);
    }

    /**
     * Ask the panel to install a one-use upload key on the host.
     *
     * The private half comes back once and is kept in this dialog only:
     * it is never stored, here or on the server, so closing the dialog
     * loses it and the remedy is another key rather than recovering one.
     */
    async requestUploadKey() {
      const d = this.state.restoreDialog;
      if (!d || d.granting) return;
      d.granting = true;
      d.error = null;
      try {
        const grant = await this.orm.call(
          "cloud.instance", "grant_restore_upload", [[this.props.instance_id]]
        );
        d.grant = grant;
        d.privateKey = grant.private_key;
      } catch (e) {
        d.error = e.data?.message || e.message || _t("Could not prepare the upload.");
      } finally {
        d.granting = false;
      }
    }

    /**
     * Ask the host what arrived through the key, and show it.
     *
     * Name, size and digest are computed on the host. Confirming them
     * is what makes a file somebody else could have placed there
     * useless: it would not be the one being restored.
     */
    async checkUpload() {
      const d = this.state.restoreDialog;
      if (!d?.grant) return;
      d.error = null;
      try {
        const jobId = await this.orm.call(
          "cloud.instance", "verify_restore_upload",
          [[this.props.instance_id], d.grant.grant_id]
        );
        if (jobId) window.open(`/cloud/log/${jobId}`, "_blank");
        const [grant] = await this.orm.call(
          "cloud.restore.upload.grant", "read",
          [[d.grant.grant_id], ["received_filename", "received_bytes", "received_sha256"]]
        );
        if (grant?.received_filename) {
          d.received = {
            filename: grant.received_filename,
            bytes: grant.received_bytes,
            sha256: grant.received_sha256,
          };
        }
      } catch (e) {
        d.error = e.data?.message || e.message || _t("Could not check the upload.");
      }
    }

    /**
     * Return the shell command that sends the backup through the key.
     *
     * ``umask 077`` before the key file is written is not decoration:
     * ssh refuses a private key other users could read, and a file
     * created without it is exactly that.
     *
     * @returns {string} a command the operator can paste as-is.
     */
    uploadCommand() {
      const d = this.state.restoreDialog;
      const g = d?.grant;
      if (!g) return "";
      const portFlag = g.port && g.port !== 22 ? ` -p ${g.port}` : "";
      return [
        `umask 077 && printf '%s' '${d.privateKey}' > ic-upload-key`,
        `rsync -P --inplace -e "ssh${portFlag} -i ic-upload-key" \\`,
        `  <your-backup.zip> ${g.user}@${g.host}:${g.directory}/`,
        `rm -f ic-upload-key`,
      ].join("\n");
    }

    /**
     * POST to a restore-upload route and return its parsed body.
     *
     * A 200 carrying something other than JSON is how a timed-out
     * session announces itself — the login page comes back with the
     * status of a success — so it is named here rather than left to
     * blow up inside the parser.
     *
     * @param {string} path Route below the instance's restore prefix.
     * @param {FormData} body Fields to send; the CSRF token is added.
     * @returns {Promise<object>} the parsed response.
     */
    async _restoreUploadCall(path, body) {
      body.append("csrf_token", odoo.csrf_token);
      const resp = await fetch(
        `/cloud/instance/${this.props.instance_id}/restore/${path}`,
        {method: "POST", body}
      );
      const ctype = (resp.headers.get("Content-Type") || "").toLowerCase();
      if (resp.ok && !ctype.includes("application/json")) {
        throw new Error(_t("Session expired. Please reload and try again."));
      }
      let payload = {};
      try {
        payload = await resp.json();
      } catch {
        payload = {};
      }
      if (!resp.ok) {
        throw new Error(payload.error || `Server error (${resp.status})`);
      }
      if (payload.error) throw new Error(payload.error);
      return payload;
    }

    /**
     * Send the selected file in pieces and enqueue the restore.
     *
     * One request per piece, each far below the size a CDN or reverse
     * proxy in front of the panel will accept — which is what the
     * single-request upload kept running into, as a 413 produced before
     * Odoo ever saw the body. A piece that fails is re-sent on its own;
     * the server keys on the offset, so re-sending one that did land is
     * harmless.
     *
     * @param {object} d The restore dialog state.
     * @returns {Promise<object>} the finish response, carrying `job_id`.
     */
    async _uploadInChunks(d) {
      const begun = await this._restoreUploadCall("begin", new FormData());
      const chunkSize = begun.chunk_bytes || 32 * 1024 * 1024;
      const total = d.file.size;
      d.progress = 0;
      let offset = 0;
      while (offset < total) {
        const piece = d.file.slice(offset, offset + chunkSize);
        const fd = new FormData();
        fd.append("upload_id", begun.upload_id);
        fd.append("offset", String(offset));
        fd.append("chunk", piece);
        let sent = null;
        for (let attempt = 0; attempt < 3 && sent === null; attempt++) {
          try {
            sent = await this._restoreUploadCall("part", fd);
          } catch (e) {
            if (attempt === 2) throw e;
            await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
          }
        }
        offset = sent.size ?? offset + piece.size;
        d.progress = Math.round((offset / total) * 100);
      }
      const fd = new FormData();
      fd.append("upload_id", begun.upload_id);
      fd.append("filename", d.file.name || "restore.zip");
      fd.append("total_size", String(total));
      fd.append(
        "backup_before_restore", d.backupBeforeRestore ? "true" : "false"
      );
      return this._restoreUploadCall("finish", fd);
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
          const result = await this._uploadInChunks(d);
          if (result.job_id) window.open(`/cloud/log/${result.job_id}`, "_blank");
        } else if (d.mode === "ssh") {
          if (!d.grant) {
            d.error = _t("Ask for an upload key first.");
            d.uploading = false;
            return;
          }
          if (!d.received) {
            d.error = _t("Check what arrived before restoring it.");
            d.uploading = false;
            return;
          }
          const jobId = await this.orm.call("cloud.instance", "restore_db", [
            [this.props.instance_id],
            {
              mode: "ssh_upload",
              grant_id: d.grant.grant_id,
              backup_before_restore: d.backupBeforeRestore,
            },
          ]);
          if (jobId) window.open(`/cloud/log/${jobId}`, "_blank");
        } else {
          if (!d.url) {
            d.error = _t("Paste the link to your backup first.");
            d.uploading = false;
            return;
          }
          const jobId = await this.orm.call(
            "cloud.instance", "restore_from_url",
            [[this.props.instance_id], d.url, d.backupBeforeRestore]
          );
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
