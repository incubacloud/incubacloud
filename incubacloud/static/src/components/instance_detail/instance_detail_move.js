/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

/**
 * Move-to-host: target picker, cutover and rollback.
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const MoveMixin = (Base) =>
  class extends Base {
    // ── Move to host ──────────────────────────────────────────────────────

    /**
     * Whether the "Move to host" action is offerable: the instance is
     * deployed, the operator may manage hosts, and no move is currently
     * in progress (a half-finished move must be rolled back first).
     *
     * @returns {boolean}
     */
    get canMoveHost() {
      return !!(
        this.state.inst?.deployed &&
        this.env.permissions?.can_manage_hosts &&
        !this.state.inst?.move_origin_host_id
      );
    }

    /**
     * Whether a rollback is actively running. Truthy when cleanup or
     * start jobs are in-flight after the user clicked "Roll back move".
     *
     * @returns {boolean}
     */

    /**
     * Whether a rollback is actively running. Truthy when cleanup or
     * start jobs are in-flight after the user clicked "Roll back move".
     *
     * @returns {boolean}
     */
    get isRollingBack() {
      return !!this.state.inst?.move_rollback_in_progress;
    }

    /**
     * Whether a move is in progress or stuck mid-move. Only truthy when
     * the backend has a marker set AND no rollback is running.
     *
     * @returns {boolean}
     */

    /**
     * Whether a move is in progress or stuck mid-move. Only truthy when
     * the backend has a marker set AND no rollback is running.
     *
     * @returns {boolean}
     */
    get isMoving() {
      return (
        !!this.state.inst?.move_origin_host_id &&
        !this.state.inst?.move_rollback_in_progress
      );
    }

    /**
     * Target-host options for the move picker, shaped for SearchSelect.
     *
     * @returns {{value: number, label: string}[]}
     */

    /**
     * Target-host options for the move picker, shaped for SearchSelect.
     *
     * @returns {{value: number, label: string}[]}
     */
    get moveHostOptions() {
      return this.state.moveHosts.map((h) => ({value: h.id, label: h.name}));
    }

    /**
     * Open the inline move picker, resetting its transient state.
     *
     * @returns {void}
     */

    /**
     * Open the inline move picker, resetting its transient state.
     *
     * @returns {void}
     */
    openMovePicker() {
      this.state.movePicker = {targetHostId: null, loading: false, loaded: false};
    }

    /**
     * Close the inline move picker.
     *
     * @returns {void}
     */

    /**
     * Close the inline move picker.
     *
     * @returns {void}
     */
    closeMovePicker() {
      this.state.movePicker = null;
    }

    /**
     * Lazily load the candidate target hosts for a move. Only hosts that
     * are ready (compatible + Traefik deployed) and different from the
     * instance's current host are kept — matching the backend, which
     * rejects non-ready or same-host targets. Idempotent: returns early
     * once a successful load has populated the list.
     *
     * @returns {Promise<void>}
     */

    /**
     * Lazily load the candidate target hosts for a move. Only hosts that
     * are ready (compatible + Traefik deployed) and different from the
     * instance's current host are kept — matching the backend, which
     * rejects non-ready or same-host targets. Idempotent: returns early
     * once a successful load has populated the list.
     *
     * @returns {Promise<void>}
     */
    async _loadMoveHosts() {
      if (!this.state.movePicker || this.state.movePicker.loaded) return;
      this.state.movePicker.loading = true;
      try {
        const resp = await rpc("/cloud/get_hosts", {});
        const allHosts = resp.hosts || resp || [];
        this.state.moveHosts = allHosts.filter(
          (h) =>
            h.status === "compatible" &&
            h.traefik_deployed &&
            h.id !== this.state.inst.host_id
        );
        if (this.state.movePicker) this.state.movePicker.loaded = true;
      } catch (e) {
        this.env.toast?.error(
          e.data?.message || e.message || _t("Failed to load hosts.")
        );
      } finally {
        if (this.state.movePicker) this.state.movePicker.loading = false;
      }
    }

    /**
     * Record the picked target host id from the SearchSelect.
     *
     * @param {string|number} value - the selected host id (may arrive as a string)
     * @returns {void}
     */

    /**
     * Record the picked target host id from the SearchSelect.
     *
     * @param {string|number} value - the selected host id (may arrive as a string)
     * @returns {void}
     */
    onMoveTargetChange(value) {
      if (this.state.movePicker) {
        this.state.movePicker.targetHostId = parseInt(value) || null;
      }
    }

    /**
     * Confirm and launch the migration to the picked target host. Guards
     * against a missing target, asks for confirmation, then enqueues the
     * backend move job and refreshes the instance on success.
     *
     * @returns {Promise<void>}
     */

    /**
     * Confirm and launch the migration to the picked target host. Guards
     * against a missing target, asks for confirmation, then enqueues the
     * backend move job and refreshes the instance on success.
     *
     * @returns {Promise<void>}
     */
    async confirmMove() {
      const targetHostId = this.state.movePicker?.targetHostId;
      if (!targetHostId) return;
      const ok = await this._confirm({
        title: _t("Move instance"),
        message: _t(
          "This migrates the instance to the selected host. The instance will be briefly unavailable (a few minutes) while data is copied; it comes back automatically."
        ),
        confirmLabel: _t("Move"),
        isDanger: false,
      });
      if (!ok) return;
      try {
        const r = await rpc("/cloud/move_instance", {
          instance_id: this.props.instance_id,
          target_host_id: targetHostId,
        });
        if (r.ok === false) {
          this.env.toast?.error(r.error || _t("Move failed"));
          return;
        }
        this.env.toast?.success(_t("Migration started."));
        this.closeMovePicker();
        this._silentRefresh();
      } catch (e) {
        this.env.toast?.error(e.data?.message || e.message || _t("Move failed"));
      }
    }

    /**
     * Confirm and roll back an in-progress (or failed mid-move) migration,
     * bringing the instance back up on its source host. Refreshes on success.
     *
     * @returns {Promise<void>}
     */

    /**
     * Confirm and roll back an in-progress (or failed mid-move) migration,
     * bringing the instance back up on its source host. Refreshes on success.
     *
     * @returns {Promise<void>}
     */
    async rollbackMove() {
      const ok = await this._confirm({
        title: _t("Roll back move"),
        message: _t(
          "Cancel the in-progress move, clean up the target host, and bring the instance back up on its source host (%s). This may take several minutes."
        ).replace("%s", this.state.inst.move_origin_host || _t("source")),
        confirmLabel: _t("Roll back"),
        isDanger: true,
      });
      if (!ok) return;
      try {
        const r = await rpc("/cloud/rollback_move", {
          instance_id: this.props.instance_id,
        });
        if (r.ok === false) {
          this.env.toast?.error(r.error || _t("Roll back failed"));
          return;
        }
        this.env.toast?.success(_t("Move rolled back."));
        this._silentRefresh();
      } catch (e) {
        this.env.toast?.error(e.data?.message || e.message || _t("Roll back failed"));
      }
    }
  };
