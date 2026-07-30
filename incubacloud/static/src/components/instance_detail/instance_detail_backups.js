/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";
import {parseUTC} from "../../utils/dates";

/**
 * Backup list, create, download and restore-from-backup.
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const BackupsMixin = (Base) =>
  class extends Base {
    // ── Backups ──────────────────────────────────────────────────────────

    get canShowBackups() {
      const inst = this.state.inst;
      return !!(inst && inst.deployed);
    }

    get isProductionBackup() {
      const inst = this.state.inst;
      return !!(
        inst &&
        inst.environment === "production" &&
        inst.effective_backup_backend_id
      );
    }

    formatBackupDate(isoStr) {
      if (!isoStr) return "—";
      const d = parseUTC(isoStr);
      if (!d || isNaN(d)) return isoStr;
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
        d.getHours()
      )}:${pad(d.getMinutes())}`;
    }

    formatSize(bytes) {
      if (!bytes) return "—";
      const units = ["B", "KB", "MB", "GB"];
      let i = 0;
      let size = bytes;
      while (size >= 1024 && i < units.length - 1) {
        size /= 1024;
        i++;
      }
      return `${size.toFixed(i ? 1 : 0)} ${units[i]}`;
    }

    async _loadBackups() {
      /**Load persisted backup records (instant, no SSH job).*/
      this.state.backupsLoading = true;
      this.state.backupsError = null;
      try {
        const res = await rpc("/cloud/list_backups", {
          instance_id: this.props.instance_id,
          offset: this.state.backupsOffset,
          limit: this.pageSize,
        });
        if (!res.ok) {
          this.state.backupsError = res.error;
          return;
        }
        this.state.backupsData = res.result;
      } catch (e) {
        this.state.backupsError =
          e.data?.message || e.message || _t("Failed to load backups.");
      } finally {
        this.state.backupsLoading = false;
      }
    }

    /** Backups paginator: jump to a page and reload. */

    /** Backups paginator: jump to a page and reload. */
    backupsGoPage(offset) {
      this.state.backupsOffset = offset;
      this._loadBackups();
    }

    async refreshBackups() {
      this.state.backupsError = null;
      this.state.backupsOffset = 0;
      const data = await this._enqueueJob(
        () =>
          rpc("/cloud/list_backups", {
            instance_id: this.props.instance_id,
            refresh: true,
          }),
        {
          loadingKey: "backupsLoading",
          goToOverview: true,
          errorLabel: _t("Failed to refresh backups"),
        }
      );
      if (data) {
        this.state.backupsJobId = data.job_id;
        this._pollBackupResult();
      }
    }

    async _pollBackupResult() {
      const jobId = this.state.backupsJobId;
      if (!jobId) return;
      const poll = async () => {
        // Component may have unmounted while awaiting the previous
        // RPC. Double-checking inside the tick complements the
        // clearTimeout inside useSafePoll: the clear kills the
        // queued callback, this kills any RPC already in flight.
        if (!this._safePoll.alive) return;
        try {
          const res = await rpc("/cloud/get_backup_result", {
            job_id: jobId,
            offset: this.state.backupsOffset,
            limit: this.pageSize,
          });
          if (!this._safePoll.alive) return;
          if (!res.ok) {
            this.state.backupsError = res.error || _t("Backup scan failed.");
            this.state.backupsLoading = false;
            return;
          }
          if (res.state === "done") {
            this.state.backupsData = res.result;
            this.state.backupsLoading = false;
            this.state.actionLoading = false;
            return;
          }
          this._safePoll.schedule(poll, 2000);
        } catch (e) {
          if (!this._safePoll.alive) return;
          this.state.backupsError = _t("Lost connection while scanning.");
          this.state.backupsLoading = false;
          this.state.actionLoading = false;
        }
      };
      this._safePoll.schedule(poll, 1500);
    }

    // ── Create Backup modal ────────────────────────────────────────
    // Replaces the legacy ``_confirm()``-based flow.  The modal IS
    // the confirmation: opens with a single click, has a Cancel
    // button, and lets the user pick ``with_filestore`` on non-prod
    // before submitting.  Production hides the filestore choice
    // (duplicity controls shape).

    // ── Create Backup modal ────────────────────────────────────────
    // Replaces the legacy ``_confirm()``-based flow.  The modal IS
    // the confirmation: opens with a single click, has a Cancel
    // button, and lets the user pick ``with_filestore`` on non-prod
    // before submitting.  Production hides the filestore choice
    // (duplicity controls shape).
    openCreateBackupModal() {
      this.state.createBackupModal = {
        withFilestore: true,
        loading: false,
      };
    }

    closeCreateBackupModal() {
      this.state.createBackupModal = null;
    }

    async doCreateBackup() {
      const m = this.state.createBackupModal;
      if (!m || m.loading) return;
      m.loading = true;
      this.state.backupsError = null;
      try {
        const data = await this._enqueueJob(
          () =>
            rpc("/cloud/create_backup", {
              instance_id: this.props.instance_id,
              with_filestore: !!m.withFilestore,
            }),
          {
            loadingKey: "backupsLoading",
            goToOverview: true,
            errorLabel: _t("Failed to create backup"),
          }
        );
        if (data) {
          this.state.backupsJobId = data.job_id;
          this._pollBackupResult();
        }
      } finally {
        this.state.createBackupModal = null;
      }
    }

    // Per-row Download.  Non-prod rows ship a ready-to-download
    // attachment, so we bypass the Neutralized/Exact modal entirely
    // and redirect to ``/web/content/<id>``.  Prod rows have no
    // attachment (the snapshot lives in S3) and still need the
    // modal so the user can pick Neutralized vs Exact + filestore.

    // Per-row Download.  Non-prod rows ship a ready-to-download
    // attachment, so we bypass the Neutralized/Exact modal entirely
    // and redirect to ``/web/content/<id>``.  Prod rows have no
    // attachment (the snapshot lives in S3) and still need the
    // modal so the user can pick Neutralized vs Exact + filestore.
    downloadRow(bk) {
      if (bk.attachment_id) {
        window.location.href = `/web/content/${bk.attachment_id}?download=true`;
        return;
      }
      this.openDownloadModal(bk.time, null);
    }

    // Download modal: Type of dump (Neutralized/Exact) × Filestore (without/with).
    // Defaults mirror odoo.sh: Neutralized + Without filestore.

    // Download modal: Type of dump (Neutralized/Exact) × Filestore (without/with).
    // Defaults mirror odoo.sh: Neutralized + Without filestore.
    openDownloadModal(backupTime, attachmentId = null) {
      this.state.downloadModal = {
        time: backupTime,
        attachmentId: attachmentId,
        mode: "neutral",
        type: "dump",
        loading: false,
      };
    }

    closeDownloadModal() {
      this.state.downloadModal = null;
    }

    get isLiveNeutralDump() {
      const d = this.state.downloadModal;
      if (!d) return false;
      const isProd = this.state.inst?.environment === "production";
      return d.mode === "neutral" && !isProd;
    }

    get isLiveExactDump() {
      // Non-prod + Exact + no pre-built attachment → backend takes a
      // live dump on demand via click-odoo-backupdb (no historical
      // snapshot store exists on staging/dev).  Used to swap the
      // "Latest available backup" copy for something accurate.
      const d = this.state.downloadModal;
      if (!d) return false;
      const isProd = this.state.inst?.environment === "production";
      return d.mode === "exact" && !isProd && !d.attachmentId;
    }

    async doDownloadBackup() {
      const d = this.state.downloadModal;
      if (!d || d.loading) return;

      // Exact + attachment shortcut: pre-built non-prod ZIPs are served
      // directly without going through a job. The attachment already
      // contains dump + filestore; the Filestore radio is ignored in
      // this case.
      if (d.mode === "exact" && d.attachmentId) {
        window.location.href = `/web/content/${d.attachmentId}?download=true`;
        this.state.downloadModal = null;
        return;
      }

      d.loading = true;
      try {
        let res;
        if (d.mode === "neutral") {
          // Non-prod has no historical DB snapshots on the host;
          // always dump the live DB before neutralizing.
          const isProd = this.state.inst?.environment === "production";
          res = await rpc("/cloud/download_backup_neutralized", {
            instance_id: this.props.instance_id,
            time: isProd ? d.time : "live",
            with_filestore: d.type === "all",
          });
        } else {
          res = await rpc("/cloud/download_backup", {
            instance_id: this.props.instance_id,
            time: d.time,
            download_type: d.type,
          });
        }
        if (!res.ok) {
          this.state.backupsError = res.error;
          d.loading = false;
          return;
        }
        this.state.downloadModal = null;
        // The download is an enqueued job whose final ZIP lands as
        // an ir.attachment on cloud.job — surface it on Overview so
        // the user sees the job and grabs the file from there.
        this._silentRefresh();
        this.setTab("overview");
      } catch (e) {
        this.state.backupsError = e.data?.message || e.message || _t("Download failed.");
        d.loading = false;
      }
    }

    // Restore confirmation for production

    // Restore confirmation for production
    openRestoreModal(backupTime) {
      this.state.restoreBackupModal = {
        time: backupTime,
        confirmName: "",
        loading: false,
      };
    }

    closeRestoreBackupModal() {
      this.state.restoreBackupModal = null;
    }

    get restoreNameMatches() {
      const m = this.state.restoreBackupModal;
      return m && m.confirmName.trim() === (this.state.inst?.name || "");
    }

    async doRestoreBackup() {
      const m = this.state.restoreBackupModal;
      if (!m || m.loading || !this.restoreNameMatches) return;
      m.loading = true;
      const time = m.time;
      const data = await this._enqueueJob(
        () => rpc("/cloud/restore_backup", {instance_id: this.props.instance_id, time}),
        {
          openLog: true,
          goToOverview: true,
          errorLabel: _t("Restore failed"),
        }
      );
      if (data) {
        this.state.restoreBackupModal = null;
      } else {
        m.loading = false;
      }
    }

    // ── Confirm + Delete ─────────────────────────────────────────────────

    /** Open the shared confirmation dialog. See utils/use_confirm.js. */
  };
