/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

/**
 * Config sync modal: diff, per-key actions and conflicts.
 *
 * Extracted from ``instance_detail.js`` (Fase 4 / B3): the
 * component had grown past 2000 lines and mixed unrelated
 * concerns. A class mixin keeps the prototype chain intact, so
 * the template still calls ``this.method()`` unchanged and this
 * split carries no behavioural risk.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const SyncMixin = (Base) =>
  class extends Base {
    // ── Sync with project ─────────────────────────────────────────────────

    async openSyncModal() {
      this.state.syncModal = {
        diff: null,
        actions: {},
        loading: true,
        applying: false,
        error: null,
      };
      try {
        const result = await rpc("/cloud/compare_sync", {
          instance_id: this.props.instance_id,
        });
        if (!result.ok) {
          this.state.syncModal.error = result.error;
          this.state.syncModal.loading = false;
          return;
        }
        this.state.syncModal.diff = result.diff;
        this.state.syncModal.actions = this._buildSyncActions(result.diff);
      } catch (e) {
        this.state.syncModal.error = _t("Failed to compare with project.");
      }
      this.state.syncModal.loading = false;
    }

    closeSyncModal() {
      this.state.syncModal = null;
    }

    _buildSyncActions(diff) {
      const actions = {pip: {}, apt: {}, repos: {}};
      // Pre-build action maps: each item gets a default action
      for (const dep of ["pip", "apt"]) {
        actions[dep] = {
          push: {}, // {line: true/false} — push to project
          pull: {}, // {line: true/false} — pull to instance
          conflicts: {}, // {name: 'instance'|'project'|null}
        };
        for (const line of diff[dep]?.only_instance || []) {
          actions[dep].push[line] = false;
        }
        for (const line of diff[dep]?.only_project || []) {
          actions[dep].pull[line] = false;
        }
        for (const c of diff[dep]?.conflicts || []) {
          actions[dep].conflicts[c.name] = null;
        }
      }
      actions.repos = {
        push: {},
        pull: {},
        conflicts: {},
      };
      for (const r of diff.repos?.only_instance || []) {
        actions.repos.push[r.url] = false;
      }
      for (const r of diff.repos?.only_project || []) {
        actions.repos.pull[r.url] = false;
      }
      for (const c of diff.repos?.conflicts || []) {
        actions.repos.conflicts[c.url] = null;
      }
      return actions;
    }

    toggleSyncAction(section, direction, key) {
      const a = this.state.syncModal.actions[section][direction];
      a[key] = !a[key];
    }

    setSyncConflict(section, name, winner) {
      this.state.syncModal.actions[section].conflicts[name] = winner;
    }

    get syncHasSelections() {
      const a = this.state.syncModal?.actions;
      if (!a) return false;
      for (const sec of ["pip", "apt", "repos"]) {
        for (const v of Object.values(a[sec].push || {})) {
          if (v) return true;
        }
        for (const v of Object.values(a[sec].pull || {})) {
          if (v) return true;
        }
        for (const v of Object.values(a[sec].conflicts || {})) {
          if (v) return true;
        }
      }
      return false;
    }

    get syncHasDifferences() {
      const d = this.state.syncModal?.diff;
      if (!d) return false;
      for (const sec of ["pip", "apt", "repos"]) {
        if (d[sec]?.only_instance?.length) return true;
        if (d[sec]?.only_project?.length) return true;
        if (d[sec]?.conflicts?.length) return true;
      }
      return false;
    }

    async applySyncActions() {
      const modal = this.state.syncModal;
      if (!modal || modal.applying) return;
      modal.applying = true;
      modal.error = null;
      try {
        // Build payload: only include checked items
        const payload = {};
        for (const sec of ["pip", "apt", "repos"]) {
          const a = modal.actions[sec];
          payload[sec] = {
            push: Object.entries(a.push)
              .filter(([, v]) => v)
              .map(([k]) => k),
            pull: Object.entries(a.pull)
              .filter(([, v]) => v)
              .map(([k]) => k),
            conflicts: Object.fromEntries(
              Object.entries(a.conflicts).filter(([, v]) => v)
            ),
          };
        }
        const result = await rpc("/cloud/apply_sync", {
          instance_id: this.props.instance_id,
          actions: payload,
        });
        if (!result.ok) {
          modal.error = result.error || _t("Sync failed.");
          modal.applying = false;
          return;
        }
        this.state.syncModal = null;
        // Reload instance data to reflect changes
        await this.load();
      } catch (e) {
        modal.error = e.data?.message || e.message || _t("Sync failed.");
        modal.applying = false;
      }
    }

    // ── Backups ──────────────────────────────────────────────────────────
  };
