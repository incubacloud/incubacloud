/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { IcModal } from "@incubacloud/components/ic_modal/ic_modal";
import { SettingsBlock } from "@incubacloud/components/settings_form/settings_form";
import {
    approvalNote,
    canRevoke,
    credentialDetail,
    credentialText,
    grantPayload,
    grantReady,
    noEndChosen,
    ownRuleActive,
    periodEnd,
} from "./auto_approval_format";

/**
 * How a rule's state reads.
 *
 * @param {string} state one of RULE_STATES
 * @returns {string} the label shown beside the rule
 */
export function ruleStateLabel(state) {
    return (
        {
            active: _t("Active"),
            paused_role: _t("Paused: the role went up"),
            paused_platform: _t("Paused by the platform"),
            expired: _t("Expired"),
            revoked: _t("Revoked"),
        }[state] || state || ""
    );
}

/**
 * What the activity says of how an audit row's call was approved.
 *
 * @param {object} entry an audit row
 * @returns {string} "auto-approved", "approved" or ""
 */
export function approvalText(entry) {
    const note = approvalNote(entry);
    if (!note) {
        return "";
    }
    return note.kind === "auto" ? _t("auto-approved") : _t("approved");
}

/**
 * What the list of rules says of a rule's charges.
 *
 * @param {object} rule a rule as the server lists it
 * @returns {string} "Included" or "Still ask"
 */
export function chargesText(rule) {
    return rule?.include_charges ? _t("Included") : _t("Still ask for approval");
}

/**
 * The fixed notice "Connect an agent" shows to the person a rule covers.
 *
 * @param {object} rule one of the person's rules
 * @returns {string} what the notice says
 */
export function ownRuleText(rule) {
    const values = { credential: credentialText(rule), date: rule.expires_at };
    if (!rule.expires_at) {
        if (ownRuleActive(rule)) {
            return _t("%(credential)s acts without asking, with no end date.", values);
        }
        return _t(
            "%(credential)s has an auto-approval rule with no end date, paused for now: it asks for approval.",
            values,
        );
    }
    if (ownRuleActive(rule)) {
        return _t("%(credential)s acts without asking until %(date)s UTC.", values);
    }
    return _t(
        "%(credential)s has an auto-approval rule until %(date)s UTC, paused for now: it asks for approval.",
        values,
    );
}

/**
 * When a rule ends, as the list of rules says it.
 *
 * @param {object} rule a rule as the server lists it
 * @returns {string} its end (UTC), or "No end date"
 */
export function ruleEndText(rule) {
    return rule?.expires_at || _t("No end date");
}

/**
 * How a credential reads where one is chosen (the grant form's list and its
 * notice): its label, then its id and when it last connected, so that two
 * with the same label are told apart.
 *
 * @param {object} item a credential the server offers
 * @returns {string} the text shown
 */
export function credentialChoiceText(item) {
    const detail = credentialDetail(item);
    if (!detail) {
        return credentialText(item);
    }
    return _t("%(credential)s [%(key)s, last connected %(date)s UTC]", {
        credential: credentialText(item),
        key: detail.key,
        date: detail.lastSeen,
    });
}

/** Rows of a rule's activity per page. */
export const ACTIVITY_PAGE = 20;

/** An empty grant form: nothing chosen beforehand, the box unticked. */
function emptyForm() {
    return {
        userId: null,
        credential: "",
        period: "",
        date: "",
        includeCharges: false,
        acknowledged: false,
        busy: false,
        error: "",
    };
}

/**
 * Settings → Agents: the auto-approval rules, the notice that grants one,
 * revoking, and what each rule approved.
 */
export class McpAutoApproval extends Component {
    static template = "incubacloud_mcp.McpAutoApproval";
    static components = { IcModal, SettingsBlock };
    static props = {};

    /** Load the rules. */
    setup() {
        this.state = useState({
            loading: true,
            loadError: "",
            data: null,
            // The grant dialog: its candidates and its form.
            grant: null,
            // The rule whose activity is open, and its page.
            activity: null,
            // The rule waiting for "Revoke" to be confirmed.
            revoking: null,
        });
        this.stateLabel = ruleStateLabel;
        this.credentialText = credentialText;
        this.credentialChoiceText = credentialChoiceText;
        this.canRevoke = canRevoke;
        this.approvalText = approvalText;
        this.chargesText = chargesText;
        this.ruleEndText = ruleEndText;
        onWillStart(() => this.load());
    }

    /** Read the rules and what the panel allows. */
    async load() {
        this.state.loading = true;
        this.state.loadError = "";
        try {
            this.state.data = await rpc("/cloud/mcp/auto_approval/list", {});
        } catch {
            this.state.loadError = _t("Failed to load the auto-approval rules.");
        } finally {
            this.state.loading = false;
        }
    }

    /** The person chosen in the grant form, with their credentials. */
    get chosenUser() {
        const grant = this.state.grant;
        return grant?.users.find((user) => user.id === grant.form.userId) || null;
    }

    /** The credential chosen in the grant form. */
    get chosenCredential() {
        const user = this.chosenUser;
        const key = this.state.grant?.form.credential;
        return user?.credentials.find((item) => item.credential === key) || null;
    }

    /** Whether the grant can be confirmed (see grantReady). */
    get canConfirm() {
        return grantReady(this.state.grant?.form);
    }

    /** The day the rule chosen would end, for the notice. */
    get endDay() {
        return periodEnd(this.state.grant?.form);
    }

    /** Whether the rule chosen has no end date. */
    get noEnd() {
        return noEndChosen(this.state.grant?.form);
    }

    /**
     * The notice's first sentence, with what was chosen; placeholders
     * until it is.
     */
    get noticeLead() {
        const user = this.chosenUser;
        const credential = this.chosenCredential;
        const values = {
            date: this.endDay || _t("the date you choose"),
            credential: credential ? credentialChoiceText(credential) : _t("the credential"),
            person: user?.name || _t("the person"),
        };
        if (this.noEnd) {
            return _t(
                "With no end date, %(credential)s of %(person)s runs every action that would wait for an approval, without asking, until someone revokes the rule.",
                values,
            );
        }
        return _t(
            "Until %(date)s, %(credential)s of %(person)s runs every action that would wait for an approval, without asking.",
            values,
        );
    }

    /** What the person's role lets the credential do without asking. */
    get noticeReach() {
        const person = this.chosenUser?.name || _t("the person");
        const reach = _t(
            "With %(person)s's role, that includes deleting projects, instances, servers and backups; restoring over production; commands on production instances and on servers; security settings and purging the audit log; sign-in links and backup downloads; writing to any branch and to public repositories.",
            { person },
        );
        if (!this.state.data?.charges) {
            return reach;
        }
        // Outside a template literal: the export of translations does not
        // read a call written inside one.
        const charges = this.state.grant?.form.includeCharges
            ? _t("It also includes what is charged to your account.")
            : _t(
                  "Actions that charge your account are not included: they still ask for approval."
              );
        return `${reach} ${charges}`;
    }

    /** The leaked credential's risk, naming the person. */
    get noticeLeak() {
        return _t(
            "Whoever holds it acts as %(person)s, without asking, until the rule ends. Revoke the rule and the credential.",
            { person: this.chosenUser?.name || _t("the person") },
        );
    }

    /** The earliest day the date picker offers: tomorrow. */
    get minDay() {
        return new Date(Date.now() + 86400000).toISOString().slice(0, 10);
    }

    /** Open the grant dialog, with every person and credential that may get a rule. */
    async openGrant() {
        let users = [];
        try {
            users = (await rpc("/cloud/mcp/auto_approval/candidates", {})).users || [];
        } catch {
            this.env.toast?.error(_t("Could not load the users and their credentials."));
            return;
        }
        this.state.grant = { users, form: emptyForm() };
    }

    /** Close the grant dialog; nothing is granted. */
    closeGrant() {
        if (!this.state.grant?.form.busy) {
            this.state.grant = null;
        }
    }

    /**
     * Choose the person; their credential is chosen again.
     *
     * @param {string} value the user's id, from the select
     */
    chooseUser(value) {
        const form = this.state.grant.form;
        form.userId = parseInt(value, 10) || null;
        form.credential = "";
        form.acknowledged = false;
    }

    /**
     * Change a field of the grant form. A change after the box was ticked
     * unticks it: the notice was read for what was chosen then.
     *
     * @param {string} field the form's field
     * @param {*} value its new value
     */
    setField(field, value) {
        const form = this.state.grant.form;
        form[field] = value;
        if (field !== "acknowledged") {
            form.acknowledged = false;
        }
    }

    /** Grant the rule, once the notice is accepted. */
    async confirmGrant() {
        const grant = this.state.grant;
        if (!this.canConfirm) {
            return;
        }
        grant.form.busy = true;
        grant.form.error = "";
        try {
            const result = await rpc(
                "/cloud/mcp/auto_approval/grant",
                grantPayload(grant.form, this.state.data.notice_version),
            );
            if (!result?.ok) {
                grant.form.error = result?.error || _t("The rule was not granted.");
                return;
            }
            this.state.grant = null;
            this.env.toast?.success(_t("Auto-approval granted."));
            await this.load();
        } catch {
            grant.form.error = _t("The rule was not granted.");
        } finally {
            if (this.state.grant) {
                this.state.grant.form.busy = false;
            }
        }
    }

    /**
     * Ask before revoking *rule*.
     *
     * @param {object} rule the rule
     */
    askRevoke(rule) {
        this.state.revoking = rule;
    }

    /** Revoke the rule asked for: the next call of its credential asks again. */
    async confirmRevoke() {
        const rule = this.state.revoking;
        this.state.revoking = null;
        if (!rule) {
            return;
        }
        try {
            await rpc("/cloud/mcp/auto_approval/revoke", { rule_id: rule.id });
            this.env.toast?.success(_t("Auto-approval revoked."));
        } catch {
            this.env.toast?.error(_t("The rule could not be revoked."));
        }
        await this.load();
        if (this.state.activity?.rule.id === rule.id) {
            await this.openActivity(rule, 0);
        }
    }

    /**
     * Show a page of what *rule* approved.
     *
     * @param {object} rule the rule
     * @param {number} offset the first row
     */
    async openActivity(rule, offset = 0) {
        try {
            const page = await rpc("/cloud/mcp/auto_approval/activity", {
                rule_id: rule.id,
                offset,
                limit: ACTIVITY_PAGE,
            });
            this.state.activity = { rule, offset, ...page };
        } catch {
            this.env.toast?.error(_t("Could not load what this rule approved."));
        }
    }

    /** Close the activity. */
    closeActivity() {
        this.state.activity = null;
    }

    /**
     * Where an activity row happened: instance, server or project.
     *
     * @param {object} entry an audit row
     * @returns {string} the name, or "—"
     */
    targetOf(entry) {
        return entry.instance_name || entry.host_name || entry.project_name || "—";
    }
}
