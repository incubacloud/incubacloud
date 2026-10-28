/** @odoo-module **/

import {_t} from "@web/core/l10n/translation";
import {rpc} from "@web/core/network/rpc";
import {announceJob} from "../../utils/job_queue";

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
        // handed us (held in memory only, never sent anywhere), whether
        // the job installing it still waits in the queue, and what the
        // host reports having received.
        granting: false,
        grant: null,
        privateKey: "",
        keyQueued: false,
        received: null,
        // The check of what arrived is a job, which may wait in the
        // queue: until it ends there is nothing to show or to restore.
        checking: false,
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
        // The key is installed by a job, which may wait behind another
        // on the instance: the dialog says so until then.
        const answer = await announceJob(this.env.toast, grant.job_id);
        d.keyQueued = answer?.state === "queued";
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
     *
     * The check is a job that only queues here: the grant is read once
     * it has ended, not before (it still held the previous check's
     * answer, and the dialog showed a file that was no longer there).
     * Until then the last answer is forgotten, so nothing can be
     * restored on it.
     */
    async checkUpload() {
      const d = this.state.restoreDialog;
      if (!d?.grant || d.checking) return;
      d.error = null;
      d.received = null;
      d.checking = true;
      try {
        const jobId = await this.orm.call(
          "cloud.instance", "verify_restore_upload",
          [[this.props.instance_id], d.grant.grant_id]
        );
        if (jobId) {
          await announceJob(this.env.toast, jobId);
          const ended = await this._untilJobEnds(jobId, d);
          if (!ended) return;
        }
        const [grant] = await this.orm.call(
          "cloud.restore.upload.grant", "read",
          [[d.grant.grant_id], ["received_filename", "received_bytes", "received_sha256"]]
        );
        // A check that found nothing usable forgets the last one: the
        // dialog must not keep showing it.
        d.received = grant?.received_filename
          ? {
              filename: grant.received_filename,
              bytes: grant.received_bytes,
              sha256: grant.received_sha256,
            }
          : null;
      } catch (e) {
        d.error = e.data?.message || e.message || _t("Could not check the upload.");
      } finally {
        d.checking = false;
      }
    }

    /**
     * Wait until a job has ended, polling its state as the rest of the
     * panel does while it waits for a job (``useSafePoll``).
     *
     * @param {number} jobId the job to wait for
     * @param {object} dialog the dialog that asked: closing it, or
     *     leaving the page, stops the wait
     * @returns {Promise<boolean>} true once the job is done, failed or
     *     cancelled; false if the wait was abandoned, or the job cannot
     *     be read
     */
    _untilJobEnds(jobId, dialog) {
      return new Promise((resolve) => {
        const poll = async () => {
          if (!this._safePoll.alive || this.state.restoreDialog !== dialog) {
            return resolve(false);
          }
          try {
            const data = await rpc("/cloud/get_job_state", {job_id: jobId});
            if (!this._safePoll.alive || this.state.restoreDialog !== dialog) {
              return resolve(false);
            }
            if (!data || data.ok === false) return resolve(false);
            if (["done", "failed", "cancelled"].includes(data.state)) {
              return resolve(true);
            }
          } catch (_e) {
            // A dropped connection is retried on the next tick.
          }
          this._safePoll.schedule(poll, 1500);
        };
        poll();
      });
    }

    /**
     * Return the shell command that sends the backup through the key.
     *
     * ``umask 077`` before the key file is written is not decoration:
     * ssh refuses a private key other users could read, and a file
     * created without it is exactly that.
     *
     * The destination is ``restore.zip``, relative: the key's ``rrsync``
     * takes every path inside the grant's directory, an absolute one too
     * (which then named a directory that does not exist), and a restore
     * only takes a file whose name is one safe component — the local
     * file's own name may have spaces.
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
        `  <your-backup.zip> ${g.user}@${g.host}:restore.zip`,
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
          if (result.job_id) await announceJob(this.env.toast, result.job_id);
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
              // The file the operator saw: the server restores no other.
              expected_sha256: d.received.sha256,
            },
          ]);
          if (jobId) await announceJob(this.env.toast, jobId);
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
          if (jobId) await announceJob(this.env.toast, jobId);
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
