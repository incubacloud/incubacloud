/**
 * archive_copy — presentation helpers for an archived instance's backup
 * copy.
 *
 * An archived instance is a record whose only remaining substance is the
 * chain sitting in the bucket. The list therefore has to be honest about
 * two different things it cannot conflate: what the last check saw, and
 * how long ago that check happened. A "Present" badge with no date reads
 * as "it is there right now", which is exactly the claim the panel is
 * not entitled to make — the reading comes from a cron, not from the
 * render.
 *
 * These live apart from the component so the branching that decides
 * whether the Revive button appears is testable on its own, without
 * mounting a project view.
 */
import { _t } from "@web/core/l10n/translation";

/** Human-readable byte count. ``—`` when there is nothing to show. */
export function formatBytes(bytes) {
    if (!bytes) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let size = bytes;
    while (size >= 1024 && i < units.length - 1) {
        size /= 1024;
        i++;
    }
    return `${size.toFixed(i ? 1 : 0)} ${units[i]}`;
}

/**
 * Label for a copy state.
 *
 * The empty state is "Not checked yet", not "Missing": an instance
 * archived a minute ago has not met the cron, and reporting that as data
 * loss would send an operator chasing a copy that is perfectly fine.
 */
export function copyStateLabel(state) {
    return {
        present: _t("Present"),
        missing: _t("Missing"),
        unreachable: _t("Unreachable"),
    }[state] || _t("Not checked yet");
}

/**
 * ``rl-sb`` badge modifier for a copy state.
 *
 * ``unreachable`` maps to warn and ``missing`` to failed on purpose: one
 * is a storage hiccup to wait out, the other is data that is gone. An
 * unchecked copy falls back to the muted ``draft`` bucket rather than to
 * either of the two, because it is neither.
 *
 * @param {string} state one of present / missing / unreachable / ""
 * @returns {string} the modifier to append to ``rl-sb``
 */
export function copyStateBadgeClass(state) {
    return {
        present: "ok",
        missing: "failed",
        unreachable: "warn",
    }[state] || "draft";
}

/**
 * Whether the Revive button should be offered for a row.
 *
 * A copy the cron has not looked at yet is still offered: ``revive``
 * re-probes live before it deploys anything, so the worst case is a
 * clear error instead of a button that is missing when it should not
 * be. What is never offered is a row with no frozen destination — there
 * is provably nothing to restore — or one the last check found gone.
 *
 * @param {object} inst row from ``/cloud/get_archived_instances``
 * @returns {boolean}
 */
export function isRevivable(inst) {
    if (!inst || !inst.has_copy) return false;
    return inst.copy_state !== "missing";
}
