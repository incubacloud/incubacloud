/** @odoo-module **/

/**
 * Where Settings learns what it shows: its groups, its sections, the blocks
 * of each section and the fields of each block.
 *
 * Settings is a menu of sections, one page each (/cloud/settings/<section>).
 * A section is the list of the blocks registered for it, in order, so a
 * module adds a section of its own, or a block to another's, the way it
 * used to add a tab. Each field is declared once, in its block: the row on
 * screen, the search index and the Settings group of Ctrl K all read that
 * declaration.
 *
 * Groups      registry "incubacloud.settings_groups"
 *     { title, sequence }
 * Sections    registry "incubacloud.settings_sections", keyed by the URL id
 *     { group, sequence, title, description, visible?(env) }
 * Blocks      registry "incubacloud.settings_blocks"
 *     { section, sequence, Component, props?, fields?, visible?(env) }
 * Elsewhere   registry "incubacloud.settings_elsewhere"
 *     { label, where, synonyms?, route, params?, visible?(env) }
 *
 * A field: { id, label, help?, synonyms?, block?, type, unit?, width?,
 *            advanced?, min?, max?, decimals?, minMessage?, validate?,
 *            options?, placeholder?, mono?, rows? }
 * ``id`` is the key of the field in its block's form and the anchor of its
 * row (#<id>), so it is unique across Settings. ``type`` is one of number,
 * text, textarea, select, toggle, secret, readonly or custom (a row the
 * block draws itself, still found by the search).
 */

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

export const settingsGroups = registry.category("incubacloud.settings_groups");
export const settingsSections = registry.category("incubacloud.settings_sections");
export const settingsBlocks = registry.category("incubacloud.settings_blocks");
export const settingsElsewhere = registry.category("incubacloud.settings_elsewhere");

/** The section Settings opens on when the URL names none. */
export const DEFAULT_SECTION = "general";

/**
 * The tabs of the Settings that had tabs, and the section each one became,
 * so links and bookmarks that name a tab still land where they meant.
 */
export const LEGACY_TABS = Object.freeze({
    general: "general",
    github: "github",
    git_providers: "git-providers",
    rates: "rates",
    monitoring: "monitoring",
    agents: "agents",
    assistant: "assistant",
    saas: "saas-instances",
    providers: "saas-providers",
});

/**
 * Whether a registry entry shows for this user and installation.
 *
 * @param {{visible?: Function}} entry a section, block or elsewhere entry
 * @param {object} env the component environment
 * @returns {boolean} true unless its ``visible`` says no
 */
function isShown(entry, env) {
    return !entry.visible || Boolean(entry.visible(env));
}

/**
 * The blocks of a section, in order, that show here.
 *
 * @param {string} sectionId the section's URL id
 * @param {object} env the component environment
 * @returns {object[]} the block entries, each with its registry ``key``
 */
export function blocksOf(sectionId, env) {
    return settingsBlocks.getEntries()
        .filter(([, block]) => block.section === sectionId && isShown(block, env))
        .map(([key, block]) => ({ key, ...block }))
        .sort((a, b) => (a.sequence ?? 50) - (b.sequence ?? 50));
}

/**
 * The menu: each group with the sections it shows, in order.
 *
 * A section shows when its own ``visible`` allows it and at least one of
 * its blocks shows, so a section a module only fills elsewhere is not an
 * empty page. Groups left with no section are left out.
 *
 * @param {object} env the component environment
 * @returns {{id: string, title: string, sections: object[]}[]}
 */
export function settingsMenu(env) {
    const sections = settingsSections.getEntries()
        .filter(([id, section]) => isShown(section, env) && blocksOf(id, env).length)
        .map(([id, section]) => ({ id, ...section }));
    return settingsGroups.getEntries()
        .map(([id, group]) => ({
            id,
            title: group.title,
            sequence: group.sequence ?? 50,
            sections: sections
                .filter((section) => section.group === id)
                .sort((a, b) => (a.sequence ?? 50) - (b.sequence ?? 50)),
        }))
        .filter((group) => group.sections.length)
        .sort((a, b) => a.sequence - b.sequence);
}

/**
 * Every field Settings shows here, with the section, group and block it
 * belongs to, in the order of the menu.
 *
 * @param {object} env the component environment
 * @returns {object[]} the fields, each with ``section``, ``sectionTitle``,
 *     ``groupTitle`` and ``blockTitle`` added
 */
export function settingsIndex(env) {
    const fields = [];
    for (const group of settingsMenu(env)) {
        for (const section of group.sections) {
            for (const block of blocksOf(section.id, env)) {
                for (const field of block.fields || []) {
                    fields.push({
                        ...field,
                        section: section.id,
                        sectionTitle: section.title,
                        groupTitle: group.title,
                        blockTitle: field.block || block.title || "",
                    });
                }
            }
        }
    }
    return fields;
}

/**
 * The pages outside Settings the search also points at.
 *
 * @param {object} env the component environment
 * @returns {object[]} the elsewhere entries that show here
 */
export function elsewhereEntries(env) {
    return settingsElsewhere.getEntries()
        .filter(([, entry]) => isShown(entry, env))
        .map(([key, entry]) => ({ key, ...entry }));
}

/**
 * A field's declaration by its id, wherever it is registered.
 *
 * @param {string} id the field's id
 * @returns {object|null} the declaration with its ``section``, or null
 */
export function settingField(id) {
    for (const [, block] of settingsBlocks.getEntries()) {
        const field = (block.fields || []).find((f) => f.id === id);
        if (field) {
            return { ...field, section: block.section, blockTitle: field.block || block.title || "" };
        }
    }
    return null;
}

/**
 * The section a Settings URL opens.
 *
 * @param {string|undefined} section the section the path names
 * @param {string|undefined} tab the ``?tab=`` of an older link
 * @returns {string} the section's id
 */
export function sectionFor(section, tab) {
    return section || LEGACY_TABS[tab] || DEFAULT_SECTION;
}

/**
 * What is wrong with a value for its field, by the field's declaration.
 *
 * Numbers must be whole unless the field takes decimals, and stay within
 * its ``min`` and ``max``; a field's own ``validate`` runs after those.
 * An empty value is left to the field: Settings has a default for each.
 *
 * @param {object} field the field's declaration
 * @param {*} value what the form holds
 * @param {object} form the whole form, for rules across fields
 * @returns {string|null} how to fix it, or null when it is fine
 */
export function fieldProblem(field, value, form = {}) {
    const problem = rawFieldProblem(field, value, form);
    // Text, not a lazy translation: it is kept and compared as text.
    return problem ? String(problem) : null;
}

/**
 * The check of ``fieldProblem``, which may answer a lazy translation.
 *
 * @param {object} field the field's declaration
 * @param {*} value what the form holds
 * @param {object} form the whole form
 * @returns {*} how to fix it, or null
 */
function rawFieldProblem(field, value, form) {
    if (field.type === "number") {
        const text = String(value ?? "").trim();
        if (text !== "") {
            const n = Number(text);
            if (!Number.isFinite(n) || (!field.decimals && !Number.isInteger(n))) {
                return field.decimals ? _t("Use a number.") : _t("Use a whole number.");
            }
            if (field.min !== undefined && n < field.min) {
                return field.minMessage || _t("Must be %s or more.", field.min);
            }
            if (field.max !== undefined && n > field.max) {
                return _t("Must be %s or less.", field.max);
            }
        }
    }
    return field.validate ? field.validate(value, form) || null : null;
}
