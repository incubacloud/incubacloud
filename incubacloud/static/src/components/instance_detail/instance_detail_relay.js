/** @odoo-module **/

import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

/** The authentication of a relay that signs in to Microsoft 365 with OAuth2. */
export const RELAY_OAUTH = "oauth2_microsoft";

/**
 * The server an OAuth2 relay always uses; the panel sets it and the form
 * shows it without letting it change (``_relay_oauth.MICROSOFT_SMTP_SERVER``).
 */
export const MICROSOFT_SMTP_SERVER = Object.freeze({
  smtp_relay_host: "smtp.office365.com",
  smtp_relay_port: 587,
  smtp_relay_security: "starttls",
});

/** The guide to OAuth2 with Microsoft 365, in the panel's documentation. */
export const RELAY_GUIDE_URL = "https://docs.incubacloud.io/instances/mail-relay/";

/**
 * A production's mail relay: how it signs in, whether it delivers, and
 * its test mail.
 *
 * With OAuth2 – Microsoft 365 the relay signs in with an app registered
 * in Entra ID instead of a password; the server is Microsoft's and the
 * user is the sender mailbox. The state comes from the health probe and
 * the test mail (``smtp_relay_state``), and the test mail goes to the
 * person who asks.
 *
 * @param {typeof import('@odoo/owl').Component} Base
 */
export const RelayMixin = (Base) =>
  class extends Base {
    /**
     * Whether the form shows the relay's authentication: production only,
     * as a staging captures its mail and never uses the relay.
     *
     * @returns {boolean}
     */
    get showsRelayAuth() {
      return this.state.form.environment === "production";
    }

    /**
     * Whether the relay, as the form has it, signs in with OAuth2.
     *
     * @returns {boolean}
     */
    get isOAuthRelay() {
      return this.showsRelayAuth && this.state.form.smtp_relay_auth === RELAY_OAUTH;
    }

    /** @returns {{value: string, label: string}[]} */
    get relayAuthOptions() {
      return [
        {value: "password", label: _t("Password")},
        {value: RELAY_OAUTH, label: _t("OAuth2 – Microsoft 365")},
      ];
    }

    /** @returns {string} */
    get relayGuideUrl() {
      return RELAY_GUIDE_URL;
    }

    /**
     * Choose how the relay signs in. OAuth2 puts Microsoft's server in
     * the form, as saving would.
     *
     * @param {string} value
     */
    onRelayAuthChange(value) {
      this.state.form.smtp_relay_auth = value;
      if (value === RELAY_OAUTH) {
        Object.assign(this.state.form, MICROSOFT_SMTP_SERVER);
      }
      this.state.saved = false;
    }

    /**
     * The relay's state as the probe or the last test mail left it, or
     * nothing for an instance whose relay is not read (staging, none).
     *
     * @returns {{tone: string, label: string, detail: string}|null}
     */
    get relayStatus() {
      const inst = this.state.inst;
      if (!inst || inst.environment !== "production" || !inst.smtp_relay_host) {
        return null;
      }
      const when = inst.smtp_relay_checked_at
        ? _t("checked %s", this.timeAgo(inst.smtp_relay_checked_at))
        : "";
      if (inst.smtp_relay_state === "ok") {
        return {tone: "ok", label: _t("Delivering"), detail: when};
      }
      if (inst.smtp_relay_state === "failing") {
        return {
          tone: "error",
          label: _t("Not delivering"),
          detail: inst.smtp_relay_error || when,
        };
      }
      return {tone: "muted", label: _t("Not checked yet"), detail: ""};
    }

    /**
     * Why "Send test mail" cannot run now: the server's reason, or the
     * form's unsaved changes, which the test would not use.
     *
     * @returns {string}
     */
    get relayTestDisabledReason() {
      if (this.hasUnsavedChanges) {
        return _t("Save the changes and apply them first.");
      }
      return this.state.inst?.relay_test_refusal || "";
    }

    /**
     * Send one test mail through the relay, to the person who asks; the
     * job says whether the relay's server took it.
     */
    async sendRelayTestMail() {
      if (this.relayTestDisabledReason) return;
      await this._enqueueJob(
        () => rpc("/cloud/instance_relay_test_mail", {
          instance_id: this.props.instance_id,
        }),
        {errorLabel: _t("Could not send the test mail"), loadingKey: "relayTestLoading"}
      );
    }
  };
