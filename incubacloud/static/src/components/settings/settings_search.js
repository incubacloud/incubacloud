/** @odoo-module **/

/**
 * The search of Settings: one index, read in the browser, behind two doors
 * (the box atop the Settings menu and the panel's Ctrl K).
 *
 * The index is the list of declared fields (settings_registry.js). A field
 * scores by where the words typed are found: the label starts with them
 * (120), the label holds them (100), a synonym (70), the title of its block,
 * section or group (50), its help (30). Case and accents do not count, and
 * nothing is searched under two characters.
 */

/** Fewest characters a search needs. */
export const MIN_QUERY = 2;
/** Results shown per section before "+N more in …". */
export const PER_SECTION = 3;

const WEIGHTS = { start: 120, label: 100, synonym: 70, title: 50, help: 30 };

/**
 * Text as the search compares it: lower case, without accents, with its
 * spaces collapsed.
 *
 * @param {*} text anything with a string form (lazy translations too)
 * @returns {string} the folded text
 */
export function fold(text) {
    return String(text ?? "")
        .normalize("NFD")
        .replace(/[̀-ͯ]/g, "")
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim();
}

/**
 * How well a field matches what was typed.
 *
 * @param {object} field an index entry (see settingsIndex)
 * @param {string} query the folded query
 * @returns {number} its score, 0 when it does not match
 */
export function scoreField(field, query) {
    const label = fold(field.label);
    if (label.startsWith(query)) {
        return WEIGHTS.start;
    }
    if (label.includes(query)) {
        return WEIGHTS.label;
    }
    if ((field.synonyms || []).some((word) => fold(word).includes(query))) {
        return WEIGHTS.synonym;
    }
    const titles = [field.blockTitle, field.sectionTitle, field.groupTitle];
    if (titles.some((title) => fold(title).includes(query))) {
        return WEIGHTS.title;
    }
    if (fold(field.help).includes(query)) {
        return WEIGHTS.help;
    }
    return 0;
}

/**
 * The fields that match, grouped by section in the order of the menu.
 *
 * @param {object[]} index the fields (settingsIndex), in menu order
 * @param {string} query what was typed
 * @param {{perSection?: number}} [options]
 * @returns {{total: number, groups: {section: string, sectionTitle: string,
 *     groupTitle: string, fields: object[], more: number}[]}}
 */
export function searchSettings(index, query, { perSection = PER_SECTION } = {}) {
    const q = fold(query);
    if (q.length < MIN_QUERY) {
        return { total: 0, groups: [] };
    }
    const groups = [];
    let total = 0;
    for (const field of index) {
        const score = scoreField(field, q);
        if (!score) {
            continue;
        }
        total += 1;
        let group = groups.find((g) => g.section === field.section);
        if (!group) {
            group = {
                section: field.section,
                sectionTitle: field.sectionTitle,
                groupTitle: field.groupTitle,
                matches: [],
            };
            groups.push(group);
        }
        group.matches.push({ field, score });
    }
    for (const group of groups) {
        // Stable: equal scores keep the order of the page.
        group.matches.sort((a, b) => b.score - a.score);
        group.fields = group.matches.slice(0, perSection).map((m) => m.field);
        group.more = group.matches.length - group.fields.length;
        delete group.matches;
    }
    return { total, groups };
}

/**
 * The entries outside Settings that match.
 *
 * @param {object[]} entries the elsewhere entries
 * @param {string} query what was typed
 * @returns {object[]} those whose label or synonyms hold the query
 */
export function searchElsewhere(entries, query) {
    const q = fold(query);
    if (q.length < MIN_QUERY) {
        return [];
    }
    return entries.filter((entry) => fold(entry.label).includes(q)
        || (entry.synonyms || []).some((word) => fold(word).includes(q)));
}

/**
 * Edit distance between two words (insertions, deletions, substitutions).
 *
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
export function editDistance(a, b) {
    let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
        const current = [i];
        for (let j = 1; j <= b.length; j++) {
            current[j] = Math.min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
            );
        }
        previous = current;
    }
    return previous[b.length];
}

/**
 * The word of the index closest to what was typed, for "Did you mean …?".
 *
 * @param {object[]} index the fields
 * @param {string} query what was typed, which matched nothing
 * @returns {string|null} a word at most two edits away, or null
 */
export function suggestion(index, query) {
    const q = fold(query);
    if (q.length < MIN_QUERY) {
        return null;
    }
    const words = new Set();
    for (const field of index) {
        for (const text of [field.label, ...(field.synonyms || [])]) {
            for (const word of fold(text).split(/[^a-z0-9]+/)) {
                if (word.length >= 3) {
                    words.add(word);
                }
            }
        }
    }
    let best = null;
    let bestDistance = 3;
    for (const word of words) {
        const distance = editDistance(q, word);
        if (distance < bestDistance && word !== q) {
            best = word;
            bestDistance = distance;
        }
    }
    return best;
}

/**
 * The parts of a label around the first place the query is in it, for
 * marking the match.
 *
 * @param {string} label the label as shown
 * @param {string} query what was typed
 * @returns {{before: string, match: string, after: string}}
 */
export function markMatch(label, query) {
    const text = String(label ?? "");
    const q = fold(query);
    // Folding keeps one character per character for the Latin letters the
    // labels use, so a position in the folded text is one in the label.
    const at = q ? fold(text).indexOf(q) : -1;
    if (at < 0 || fold(text).length !== text.length) {
        return { before: text, match: "", after: "" };
    }
    return {
        before: text.slice(0, at),
        match: text.slice(at, at + q.length),
        after: text.slice(at + q.length),
    };
}
