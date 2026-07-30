/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

/**
 * Connect-as and container shell dialogs.
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const AccessMixin = (Base) =>
  class extends Base {
    // ── Connect as user ──────────────────────────────────────────────────

    /**
     * Whether the current user may open the connect-as dialog at all.
     * Production requires Developer+; staging keeps the Stakeholder floor.
     * The backend enforces the same rule — this only keeps the UI honest.
     *
     * @returns {boolean}
     */
    get canConnectAs() {
      const perms = this.env.permissions || {};
      if (!perms.can_connect_as) return false;
      if (this.state.inst?.environment === "production") {
        return !!perms.can_connect_production;
      }
      return true;
    }

    /**
     * Whether a specific target user may be impersonated. Only the
     * combination "tenant admin + production instance" is restricted,
     * and it requires Manager.
     *
     * @param {{is_admin?: boolean}} user entry from /cloud/get_instance_users
     * @returns {boolean}
     */

    /**
     * Whether a specific target user may be impersonated. Only the
     * combination "tenant admin + production instance" is restricted,
     * and it requires Manager.
     *
     * @param {{is_admin?: boolean}} user entry from /cloud/get_instance_users
     * @returns {boolean}
     */
    canConnectAsTarget(user) {
      if (!user?.is_admin) return true;
      if (this.state.inst?.environment !== "production") return true;
      return !!(this.env.permissions || {}).can_connect_production_admin;
    }

    async openConnectModal() {
      this.state.connectDialog = {
        inst: this.state.inst,
        loading: true,
        users: [],
        error: null,
        connecting: false,
      };
      try {
        const result = await rpc("/cloud/get_instance_users", {
          instance_id: this.props.instance_id,
        });
        if (!result.ok) {
          this.state.connectDialog.error = result.error || _t("Failed to load users.");
        } else {
          this.state.connectDialog.users = result.users;
        }
      } catch (e) {
        this.state.connectDialog.error = _t("Connection error.");
      } finally {
        this.state.connectDialog.loading = false;
      }
    }

    closeConnectModal() {
      this.state.connectDialog = null;
    }

    async connectAs(userId) {
      const d = this.state.connectDialog;
      if (!d || d.connecting) return;
      const target = (d.users || []).find((u) => u.id === userId);
      if (!this.canConnectAsTarget(target)) {
        d.error = _t(
          "Connecting as an administrator of a production instance requires the Manager role.",
        );
        return;
      }
      d.connecting = true;
      d.error = null;
      try {
        const userEntry = target;
        const userName = userEntry ? userEntry.name : String(userId);
        const result = await rpc("/cloud/prepare_instance_connect", {
          instance_id: this.props.instance_id,
          user_id: userId,
          user_name: userName,
        });
        if (!result.ok) {
          d.error = result.error || _t("Failed to create session.");
          d.connecting = false;
          return;
        }
        window.open(result.url, "_blank");
        this.state.connectDialog = null;
      } catch (e) {
        d.error = _t("Connection error.");
        d.connecting = false;
      }
    }

    // ── Shell terminal ───────────────────────────────────────────────────

    // ── Shell terminal ───────────────────────────────────────────────────

    openShellDialog() {
      const inst = this.state.inst;
      const raw = inst.compose_services || "odoo,db";
      const services = Array.isArray(raw)
        ? raw
        : raw
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
      if (services.length === 1) {
        this._openShellFor(services[0]);
      } else {
        this.state.shellDialog = {inst, services, opening: false, error: null};
      }
    }

    closeShellDialog() {
      this.state.shellDialog = null;
    }

    async openShell(service) {
      const d = this.state.shellDialog;
      if (!d || d.opening) return;
      await this._openShellFor(service);
    }

    async _openShellFor(service) {
      if (this.state.shellDialog) this.state.shellDialog.opening = true;
      try {
        const result = await rpc("/cloud/terminal/open", {
          instance_id: this.props.instance_id,
          service,
        });
        if (result.ok) {
          window.open(`/cloud/terminal/${result.session_id}`, "_blank");
          this.state.shellDialog = null;
        } else {
          if (this.state.shellDialog) {
            this.state.shellDialog.error = result.error || _t("Failed to open terminal.");
            this.state.shellDialog.opening = false;
          }
        }
      } catch (e) {
        if (this.state.shellDialog) {
          this.state.shellDialog.error = _t("Connection error.");
          this.state.shellDialog.opening = false;
        }
      }
    }

    // ── Sync with project ─────────────────────────────────────────────────
  };
