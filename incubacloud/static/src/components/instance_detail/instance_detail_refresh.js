/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

/**
 * Refresh from production: pick a source, a data source and whether to
 * neutralize, then hand off to the two-job backend chain.
 *
 * odoo.sh calls this a staging rebuild. Here "rebuild" already means
 * ``copier update`` + image rebuild, so the feature is named after what
 * it does: replace this instance's database and filestore with a copy of
 * its production's, keeping the staging's own code.
 *
 * Same mixin shape as ``MoveMixin`` — a class mixin keeps the prototype
 * chain intact so the template calls ``this.method()`` unchanged.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const RefreshMixin = (Base) =>
  class extends Base {
    /**
     * Whether the "Refresh from production" action is offerable: a
     * deployed non-production instance, and an operator allowed to move
     * production data into a staging (Developer+, same gate as cloning).
     *
     * @returns {boolean}
     */
    get canRefreshFromProd() {
      return !!(
        this.state.inst?.deployed &&
        this.state.inst?.environment !== "production" &&
        this.env.permissions?.can_clone_to_staging
      );
    }

    /**
     * Deployed production instances of this project, shaped for RlSelect.
     *
     * @returns {{value: number, label: string}[]}
     */
    get refreshSourceOptions() {
      return this.state.refreshSources.map((i) => ({
        value: i.id,
        label: i.name,
      }));
    }

    /**
     * The production instance currently picked as the source, if any.
     *
     * @returns {object|undefined}
     */
    get refreshSelectedSource() {
      const id = this.state.refreshDialog?.sourceId;
      return this.state.refreshSources.find((i) => i.id === id);
    }

    /**
     * Whether the picked source can serve a duplicity snapshot. Without
     * a backup backend the only possible source is a live dump — the
     * backend degrades to it anyway, and the dialog says so up front
     * instead of letting the user pick something that silently changes.
     *
     * @returns {boolean}
     */
    get refreshCanUseBackup() {
      return !!this.refreshSelectedSource?.effective_backup_backend_id;
    }

    /**
     * Open the refresh dialog and kick off loading the candidate sources.
     *
     * @returns {void}
     */
    openRefreshDialog() {
      this.state.refreshDialog = {
        sourceId: null,
        source: "backup",
        neutralize: true,
        loading: false,
        loaded: false,
        error: null,
        submitting: false,
      };
      this._loadRefreshSources();
    }

    /**
     * Close the refresh dialog.
     *
     * @returns {void}
     */
    closeRefreshDialog() {
      this.state.refreshDialog = null;
    }

    /**
     * Load this project's deployed production instances. Idempotent:
     * returns early once a successful load has populated the list. When
     * exactly one candidate exists it is preselected, so the common case
     * (one production per project) needs no picking.
     *
     * @returns {Promise<void>}
     */
    async _loadRefreshSources() {
      if (!this.state.refreshDialog || this.state.refreshDialog.loaded) return;
      this.state.refreshDialog.loading = true;
      try {
        const resp = await rpc("/cloud/get_project_instances", {
          project_id: this.props.project_id,
        });
        this.state.refreshSources = (resp.instances || []).filter(
          (i) => i.environment === "production" && i.deployed
        );
        if (!this.state.refreshDialog) return;
        this.state.refreshDialog.loaded = true;
        if (this.state.refreshSources.length === 1) {
          this.state.refreshDialog.sourceId = this.state.refreshSources[0].id;
        }
        this._syncRefreshSourceMode();
      } catch (e) {
        if (this.state.refreshDialog) {
          this.state.refreshDialog.error =
            e.data?.message || e.message || _t("Failed to load instances.");
        }
      } finally {
        if (this.state.refreshDialog) this.state.refreshDialog.loading = false;
      }
    }

    /**
     * Record the picked source instance and re-check the data-source mode.
     *
     * @param {string|number} value - the selected instance id
     * @returns {void}
     */
    onRefreshSourceChange(value) {
      if (!this.state.refreshDialog) return;
      this.state.refreshDialog.sourceId = parseInt(value) || null;
      this._syncRefreshSourceMode();
    }

    /**
     * Force the data source to "live" when the picked production has no
     * backup backend, so the dialog never offers a snapshot that does
     * not exist.
     *
     * @returns {void}
     */
    _syncRefreshSourceMode() {
      if (!this.state.refreshDialog) return;
      if (!this.refreshCanUseBackup) {
        this.state.refreshDialog.source = "live";
      }
    }

    /**
     * Pick the data source (latest snapshot vs live dump).
     *
     * @param {string} value - 'backup' or 'live'
     * @returns {void}
     */
    setRefreshSourceMode(value) {
      if (!this.state.refreshDialog) return;
      if (value === "backup" && !this.refreshCanUseBackup) return;
      this.state.refreshDialog.source = value;
    }

    /**
     * Confirm and launch the refresh. Destructive by design — it
     * overwrites this instance's database and filestore — so it goes
     * through the panel's own confirmation dialog first.
     *
     * @returns {Promise<void>}
     */
    async doRefreshFromProduction() {
      const dlg = this.state.refreshDialog;
      if (!dlg?.sourceId || dlg.submitting) return;
      const sourceName = this.refreshSelectedSource?.name || _t("production");
      const ok = await this._confirm({
        title: _t("Refresh from production"),
        message: _t(
          "This overwrites the database and filestore of %(target)s with a copy of %(source)s. The current data is lost.",
          {target: this.state.inst.name, source: sourceName}
        ),
        confirmLabel: _t("Refresh"),
        isDanger: true,
      });
      if (!ok) return;
      dlg.submitting = true;
      try {
        const r = await rpc("/cloud/refresh_from_production", {
          instance_id: this.props.instance_id,
          source_instance_id: dlg.sourceId,
          source: dlg.source,
          neutralize: dlg.neutralize,
        });
        if (r.ok === false) {
          dlg.error = r.error || _t("Refresh failed");
          return;
        }
        this.env.toast?.success(
          r.source === "live" && dlg.source === "backup"
            ? _t("Refresh started with a live dump: production has no backup destination.")
            : _t("Refresh started.")
        );
        this.closeRefreshDialog();
        this._silentRefresh();
      } catch (e) {
        dlg.error = e.data?.message || e.message || _t("Refresh failed");
      } finally {
        if (this.state.refreshDialog) {
          this.state.refreshDialog.submitting = false;
        }
      }
    }
  };
