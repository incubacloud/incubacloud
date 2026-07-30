/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

/**
 * Audit log tab: load, filter, page and purge.
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const AuditMixin = (Base) =>
  class extends Base {
    async _loadAuditLog() {
      this.state.auditLogLoading = true;
      const f = this.state.auditFilter;
      try {
        const res = await rpc("/cloud/get_audit_log", {
          instance_id: this.props.instance_id,
          offset: this.state.auditOffset,
          limit: this.pageSize,
          q: f.q || null,
          action_filter: f.action || null,
          date_from: f.date_from || null,
          date_to: f.date_to || null,
        });
        this.state.auditLog = res.entries || [];
        this.state.auditTotal = res.total || 0;
        this.state.auditActions = res.actions || [];
      } catch (_e) {
        console.warn("Failed to load audit log:", _e);
      }
      this.state.auditLogLoading = false;
    }

    applyAuditFilter() {
      this.state.auditOffset = 0;
      this._loadAuditLog();
    }

    /** Audit-log paginator: jump to a page and reload. */

    /** Audit-log paginator: jump to a page and reload. */
    auditGoPage(offset) {
      this.state.auditOffset = offset;
      this._loadAuditLog();
    }

    resetAuditFilter() {
      this.state.auditFilter = {q: "", action: "", date_from: "", date_to: ""};
      this.state.auditOffset = 0;
      this._loadAuditLog();
    }

    async purgeAuditLogs() {
      this.state.auditPurging = true;
      try {
        const result = await rpc("/cloud/purge_audit_logs", {});
        if (result.ok) {
          this.env.toast?.success(
            result.deleted
              ? _t("Deleted %s old audit log entries").replace("%s", result.deleted)
              : _t("No old entries to purge")
          );
          this.state.auditOffset = 0;
          this._loadAuditLog();
        }
      } catch (_) {
        this.env.toast?.error(_t("Failed to purge audit logs"));
      }
      this.state.auditPurging = false;
    }
  };
