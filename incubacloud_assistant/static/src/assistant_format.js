/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/** States in which a step is queued or runs, or the task waits for a job. */
export const BUSY_STATES = ["working", "waiting_job"];
/** States in which Stop applies. */
const STOPPABLE = ["working", "waiting_job", "waiting_confirmation"];
/** States in which Retry applies. */
const RETRIABLE = ["error", "stopped"];
/** A link in the model's text, up to a space or a closing bracket. */
const URL_RE = /https?:\/\/[^\s<>"')\]]+/g;
/** Settings sent as they are when they changed. */
const PLAIN_SETTINGS = ["enabled", "provider", "default_model", "monthly_limit", "step_cap"];
/** Settings that are sets: their order does not count. */
const SET_SETTINGS = ["allowed_models", "user_ids"];

/** Hosts whose links are always clickable: the documentation and GitHub. */
export const LINK_HOSTS = ["docs.incubacloud.io", "github.com"];

/**
 * Whether the top bar shows the button, the count on it, and whether the
 * panel only reads: switched off for the person, their conversations stay
 * to read and delete until they expire.
 */
export function buttonView(status) {
    const available = Boolean(status?.available);
    const visible = available || status?.conversations > 0;
    return {
        visible,
        readOnly: visible && !available,
        badge: available && status.unread > 0 ? status.unread : 0,
    };
}

/** An amount in euros, in the person's locale. */
export function money(amount, locale) {
    return new Intl.NumberFormat(locale || undefined, {
        style: "currency",
        currency: "EUR",
    }).format(amount || 0);
}

/** What the month cost the person, and of what limit. */
export function usageLabel(usage, locale) {
    if (!usage) {
        return "";
    }
    const spent = money(usage.cost, locale);
    let text = String(
        usage.limit > 0
            ? _t("%(spent)s of %(limit)s this month", { spent, limit: money(usage.limit, locale) })
            : _t("%(spent)s this month", { spent })
    );
    if (usage.unpriced) {
        text += " " + _t("(some costs unknown)");
    }
    return text;
}

/**
 * The allowed models as the selector lists them.
 *
 * A model the server marks ``usable: false`` (no known price while the panel
 * has a monthly limit) is listed disabled, and its tag says why.
 */
export function modelOptions(models) {
    return (models || []).map((model) => {
        const disabled = model.usable === false;
        let tag = model.recommended ? _t("Recommended") : _t("Untested");
        if (disabled) {
            tag = _t("Price unknown");
        }
        return {
            id: model.id,
            label: model.name || model.id,
            recommended: Boolean(model.recommended),
            disabled,
            tag: String(tag),
        };
    });
}

/** What the panel shows of a conversation's state, and which buttons. */
export function stateView(state) {
    const labels = {
        working: _t("Working…"),
        waiting_job: _t("Waiting for a job…"),
        waiting_confirmation: _t("Waiting for your confirmation"),
        stopped: _t("Stopped"),
        error: _t("Something went wrong"),
    };
    return {
        label: String(labels[state] || ""),
        canStop: STOPPABLE.includes(state),
        canRetry: RETRIABLE.includes(state),
        busy: BUSY_STATES.includes(state),
    };
}

/** How the chat shows one message, or null when it shows nothing. */
export function messageView(message) {
    switch (message?.role) {
        case "user":
            return { kind: "user", text: message.text, queued: Boolean(message.pending) };
        case "assistant":
            return message.text ? { kind: "assistant", text: message.text, cost: message.cost } : null;
        case "tool":
            return { kind: "step", label: message.label, error: Boolean(message.is_error) };
        case "job":
            return { kind: "step", label: message.label, error: false };
        case "notice":
            return {
                kind: "notice",
                text: message.text,
                error: Boolean(message.is_error),
                askContinue: Boolean(message.ask_continue),
            };
        case "card":
            return {
                kind: "card",
                id: message.id,
                text: message.text,
                production: Boolean(message.card?.production),
                state: message.card?.state,
                open: message.card?.state === "pending",
            };
        default:
            return null;
    }
}

/** Merge a bus event into the open conversation's view; the same view if not its. */
export function mergeEvent(view, event) {
    if (!event || event.conversation_id !== view.conversationId) {
        return view;
    }
    if (event.kind === "progress") {
        return { ...view, progress: event.label || "" };
    }
    if (event.kind === "message" && event.message) {
        const known = view.messages.some((message) => message.id === event.message.id);
        const messages = known
            ? view.messages.map((message) => (message.id === event.message.id ? event.message : message))
            : [...view.messages, event.message];
        return { ...view, messages };
    }
    if (event.kind === "state") {
        return {
            ...view,
            state: event.state,
            progress: BUSY_STATES.includes(event.state) ? view.progress : "",
        };
    }
    return view;
}

/**
 * Whether *url* goes to one of *hosts*, each by its own name: a subdomain of
 * one is another host (on a server with a wildcard, another customer's).
 */
function linkAllowed(url, hosts) {
    let host = "";
    try {
        host = new URL(url).hostname.toLowerCase();
    } catch {
        return false;
    }
    return hosts.includes(host);
}

/**
 * Split *text* into plain parts and links. Only addresses on *hosts* are
 * links: any other stays text, so an address the model copied from a log
 * is never one click from sending something away.
 */
export function linkify(text, hosts = LINK_HOSTS) {
    const source = text || "";
    const parts = [];
    let last = 0;
    for (const match of source.matchAll(URL_RE)) {
        const url = match[0].replace(/[.,;:!?]+$/, "");
        if (match.index > last) {
            parts.push({ text: source.slice(last, match.index) });
        }
        parts.push(linkAllowed(url, hosts) ? { text: url, url } : { text: url });
        last = match.index + url.length;
    }
    if (last < source.length) {
        parts.push({ text: source.slice(last) });
    }
    return parts;
}

/**
 * The provider as the notices name it. OpenRouter forwards what it receives to
 * the company of the model chosen, so the notices say so.
 */
export function providerWording(status) {
    if (!status?.provider_label) {
        return "";
    }
    if (status.provider === "openrouter") {
        return String(_t("OpenRouter and the company behind the selected model"));
    }
    return status.provider_label;
}

/** The first-use notice's title. */
export function noticeTitle() {
    return String(_t("Before you use the assistant"));
}

/** The first-use notice: an AI, what goes to the provider, and how long it is kept. */
export function noticeText(provider) {
    return String(
        _t(
            "The assistant is an AI, not a person, and it can make mistakes: check its answers and its changes before you rely on them. What you write and what it reads in the panel (logs, settings, code) is sent to %(provider)s, the provider of your panel's AI key, under your organisation's contract with it. Actions that delete data, deploy to production or change sensitive settings wait for your confirmation. Only you can see your conversations, and each one is deleted 30 days after its last activity.",
            { provider }
        )
    );
}

/** The fixed label next to the message field: an AI, and where the messages go. */
export function chatLabel(provider) {
    return String(
        _t(
            "You are chatting with an AI, not a person. It can make mistakes. Your messages and what it reads are sent to %(provider)s.",
            { provider }
        )
    );
}

/** The screen the person has open, as ids only (the server names them). */
export function screenContext(route) {
    const params = route?.params || {};
    const context = { route: route?.route || "" };
    for (const key of ["project_id", "instance_id", "host_id"]) {
        if (Number.isInteger(params[key])) {
            context[key] = params[key];
        }
    }
    return context;
}

/** What Settings → Assistant sends: what changed, the key only when typed. */
export function settingsChanges(loaded, form) {
    const vals = {};
    for (const key of PLAIN_SETTINGS) {
        if (loaded[key] !== form[key]) {
            vals[key] = form[key];
        }
    }
    const sorted = (list) => JSON.stringify([...(list || [])].sort());
    for (const key of SET_SETTINGS) {
        if (sorted(loaded[key]) !== sorted(form[key])) {
            vals[key] = form[key];
        }
    }
    if (form.api_key) {
        // A key typed after "Remove the key" replaces it: the typed one wins.
        vals.api_key = form.api_key;
    } else if (form.clear_api_key) {
        vals.clear_api_key = true;
    }
    return vals;
}

/** A closed card's state, as the chat says it. */
export function cardStateLabel(state) {
    const labels = {
        confirmed: _t("Confirmed"),
        cancelled: _t("Cancelled"),
        expired: _t("Expired"),
    };
    return String(labels[state] || "");
}

/** A cloud role by the name the panel shows. */
export function roleLabel(role) {
    const labels = {
        group_cloud_user: _t("Stakeholder"),
        group_cloud_consultant: _t("Consultant"),
        group_cloud_project_manager: _t("Project manager"),
        group_cloud_developer: _t("Developer"),
        group_cloud_manager: _t("Manager"),
    };
    return String(labels[role] || "");
}
