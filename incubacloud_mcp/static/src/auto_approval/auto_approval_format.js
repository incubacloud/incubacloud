/** @odoo-module **/

/**
 * What the screens of auto-approval decide: pure functions, without the
 * words (the components translate them), so hoot checks each one.
 * Auto-approval lets one credential of one person act without asking
 * until a date (Settings → Agents, issue #8).
 */

/** The states a rule can be in, as the server names them. */
export const RULE_STATES = ["active", "paused_role", "paused_platform", "expired", "revoked"];

/**
 * Whether a rule can still be revoked: it has not ended.
 *
 * @param {{state: string}} rule a rule as the server lists it
 * @returns {boolean} true while it is active or paused
 */
export function canRevoke(rule) {
    return Boolean(rule) && !["expired", "revoked"].includes(rule.state);
}

/**
 * How a credential reads in the lists: its label, and the client's own
 * name when it declared one.
 *
 * @param {{label?: string, credential_label?: string, client_name?: string}} item
 *     a credential or a rule
 * @returns {string} the text shown
 */
export function credentialText(item) {
    const label = item?.credential_label || item?.label || "";
    return item?.client_name ? `${label} (${item.client_name})` : label;
}

/**
 * The period chosen in the grant form, as the server takes it: days, or
 * the last day.
 *
 * @param {{period: string, date: string}} form the form's period ("1",
 *     "7", "30", "90" or "date") and its date
 * @returns {{days?: number, expires_on?: string}|null} null while none is chosen
 */
export function periodPayload(form) {
    if (!form?.period) {
        return null;
    }
    if (form.period === "date") {
        return form.date ? { expires_on: form.date } : null;
    }
    const days = parseInt(form.period, 10);
    return Number.isFinite(days) ? { days } : null;
}

/**
 * Whether the grant form can be confirmed: a person, one of their
 * credentials, a period, and the notice's box ticked. Nothing is chosen
 * beforehand, so the button starts disabled.
 *
 * @param {{userId: number|null, credential: string, period: string,
 *     date: string, acknowledged: boolean, busy?: boolean}} form the form
 * @returns {boolean} true once everything is there
 */
export function grantReady(form) {
    return Boolean(
        form &&
            !form.busy &&
            form.userId &&
            form.credential &&
            periodPayload(form) &&
            form.acknowledged === true
    );
}

/**
 * The request the grant form sends.
 *
 * @param {object} form the grant form (see grantReady)
 * @param {string} noticeVersion the version of the notice shown
 * @returns {object} the route's parameters
 */
export function grantPayload(form, noticeVersion) {
    return {
        user_id: form.userId,
        credential: form.credential,
        ...periodPayload(form),
        acknowledged: form.acknowledged === true,
        notice_version: noticeVersion,
    };
}

/**
 * The date a period ends on, to say it in the notice before granting.
 *
 * @param {object} form the grant form
 * @param {Date} [now] the time it is now
 * @returns {string} the day, as YYYY-MM-DD, or "" while none is chosen
 */
export function periodEnd(form, now = new Date()) {
    const payload = periodPayload(form);
    if (!payload) {
        return "";
    }
    if (payload.expires_on) {
        return payload.expires_on;
    }
    const end = new Date(now.getTime() + payload.days * 86400000);
    return end.toISOString().slice(0, 10);
}

/**
 * How an audit row's or a job's call was approved, for the mark the panel
 * shows after the agent.
 *
 * @param {{approval?: string, approval_rule_id?: number,
 *     approval_granted_by?: string}} entry an audit row or a job
 * @returns {{kind: "auto"|"person", rule: number|null, by: string}|null}
 *     null when nobody approved it
 */
export function approvalNote(entry) {
    if (entry?.approval === "auto") {
        return {
            kind: "auto",
            rule: entry.approval_rule_id || null,
            by: entry.approval_granted_by || "",
        };
    }
    if (entry?.approval === "person") {
        return { kind: "person", rule: null, by: "" };
    }
    return null;
}

/**
 * Whether one of the person's own rules acts now, for "Connect an agent":
 * active, rather than paused.
 *
 * @param {{state: string}} rule one of the person's rules
 * @returns {boolean} true while it lets the credential act without asking
 */
export function ownRuleActive(rule) {
    return rule?.state === "active";
}
