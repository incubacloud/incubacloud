/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";
import {parseUTC} from "../../utils/dates";

/**
 * The mail a staging captured instead of sending.
 *
 * A staging never mails anybody: the doodba template wires Odoo to an
 * in-stack catcher so a copy of production cannot reach real
 * customers. That has always been true and was never visible — you had
 * to trust it, or go and look on the host. This is the tab Odoo.sh
 * calls "Mails".
 *
 * Nothing here is stored in the panel: every read goes to the host and
 * what comes back lives only in this component's state, because these
 * are somebody else's password resets.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const MailsMixin = (Base) =>
  class extends Base {
    // ── Visibility ───────────────────────────────────────────────────────

    /**
     * Whether the Mails tab applies to this instance at all.
     *
     * Production has no catcher — its mail is real — so the tab is
     * absent rather than empty. Deployment and run state are *not*
     * part of this: a stopped staging still has a Mails tab, and it
     * says why it cannot show anything.
     *
     * @returns {boolean}
     */
    get canShowMails() {
      const inst = this.state.inst;
      return !!(
        inst &&
        inst.environment !== "production" &&
        this.env.permissions?.can_view_logs
      );
    }

    /**
     * The sentence shown above the list, which differs by environment.
     *
     * Built in JS rather than split across the template so the
     * translators get one whole sentence.
     *
     * @returns {string}
     */
    get mailCaptureNotice() {
      return this.state.inst?.environment === "production"
        ? _t("Outgoing email is real on production.")
        : _t("Outgoing email on staging never leaves this server.");
    }

    // ── Listing ──────────────────────────────────────────────────────────

    /**
     * Load the mailbox listing, replacing whatever is on screen.
     *
     * @returns {Promise<void>}
     */
    async loadMails() {
      const inst = this.state.inst;
      if (!inst) return;
      this.state.mails.loading = true;
      this.state.mails.error = "";
      try {
        const res = await rpc("/cloud/instance_mailbox", {
          instance_id: inst.id,
        });
        if (res.ok) {
          this.state.mails.items = res.items || [];
          this.state.mails.total = res.total || 0;
        } else {
          this.state.mails.items = [];
          this.state.mails.total = 0;
          this.state.mails.error = res.error || _t("Could not read the mailbox");
        }
      } catch {
        this.state.mails.error = _t("Could not read the mailbox");
      } finally {
        this.state.mails.loading = false;
        this.state.mails.loaded = true;
      }
    }

    /**
     * Open one message, fetching its body and attachment list.
     *
     * @param {string} messageId MailHog id from the listing
     * @returns {Promise<void>}
     */
    async openMail(messageId) {
      const inst = this.state.inst;
      if (!inst || !messageId) return;
      this.state.mails.openId = messageId;
      this.state.mails.message = null;
      this.state.mails.messageLoading = true;
      this.state.mails.messageError = "";
      try {
        const res = await rpc("/cloud/instance_mail", {
          instance_id: inst.id,
          message_id: messageId,
        });
        if (res.ok) {
          this.state.mails.message = res;
          // Default to whichever body the message actually has, so a
          // text-only notification does not open on an empty pane.
          this.state.mails.view = res.html ? "html" : "text";
        } else {
          this.state.mails.messageError =
            res.error || _t("Could not read the message");
        }
      } catch {
        this.state.mails.messageError = _t("Could not read the message");
      } finally {
        this.state.mails.messageLoading = false;
      }
    }

    /**
     * Close the message pane and go back to the listing.
     */
    closeMail() {
      this.state.mails.openId = null;
      this.state.mails.message = null;
      this.state.mails.messageError = "";
    }

    /**
     * Switch the open message between its HTML and plain-text bodies.
     *
     * @param {"html"|"text"} view which body to render
     */
    setMailView(view) {
      this.state.mails.view = view;
    }

    /**
     * Empty the catcher after confirming, then reload the listing.
     *
     * @returns {Promise<void>}
     */
    async clearMailbox() {
      const inst = this.state.inst;
      if (!inst) return;
      const confirmed = await this._confirm({
        title: _t("Clear mailbox"),
        message: _t(
          "Delete every message this staging has captured? The catcher keeps them in memory only, so they are gone either way when it restarts.",
        ),
        confirmLabel: _t("Clear mailbox"),
        isDanger: true,
      });
      if (!confirmed) return;
      this.state.mails.loading = true;
      try {
        const res = await rpc("/cloud/instance_mailbox_clear", {
          instance_id: inst.id,
        });
        if (!res.ok) {
          this.env.toast?.error(res.error || _t("Could not clear the mailbox"));
          return;
        }
        this.closeMail();
        this.env.toast?.success(_t("Mailbox cleared."));
        await this.loadMails();
      } catch {
        this.env.toast?.error(_t("Could not clear the mailbox"));
      } finally {
        this.state.mails.loading = false;
      }
    }

    // ── Formatting ───────────────────────────────────────────────────────

    /**
     * Render a MailHog timestamp in the reader's local time.
     *
     * MailHog stamps in RFC 3339 with nanoseconds, which ``Date`` will
     * not parse; the fractional part is cut back to milliseconds first.
     *
     * @param {string} isoStr value of a message's ``created``
     * @returns {string}
     */
    formatMailDate(isoStr) {
      if (!isoStr) return "—";
      const trimmed = isoStr.replace(/(\.\d{3})\d+/, "$1");
      const d = parseUTC(trimmed) || new Date(trimmed);
      if (!d || isNaN(d)) return isoStr;
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(
        d.getDate(),
      )} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    /**
     * Render a byte count the way a mail client does.
     *
     * @param {number} size bytes
     * @returns {string}
     */
    formatMailSize(size) {
      const n = Number(size || 0);
      if (n < 1024) return `${n} B`;
      if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
      return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    }

    /**
     * Render a message's recipients as one line.
     *
     * @param {string[]} to addresses from the listing
     * @returns {string}
     */
    formatMailTo(to) {
      const list = to || [];
      if (!list.length) return "—";
      if (list.length <= 2) return list.join(", ");
      return _t("%(first)s and %(count)s more", {
        first: list[0],
        count: list.length - 1,
      });
    }
  };
