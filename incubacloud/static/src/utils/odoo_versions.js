/** @odoo-module **/

import { rpc } from "@web/core/network/rpc";

let _promise = null;

/**
 * Fetch what the backend says of the supported Odoo series.
 *
 * Single source of truth: the ``odoo_version`` Selection on the models
 * and each series' card (served by ``/cloud/get_odoo_versions``). The
 * result is cached for the page lifetime so every picker shares a single
 * request.
 *
 * @returns {Promise<{odoo_versions: string[], facts: Object}>}
 */
function fetchVersionInfo() {
    if (!_promise) {
        _promise = rpc("/cloud/get_odoo_versions")
            .then((res) => ({
                odoo_versions: (res && res.odoo_versions) || [],
                facts: (res && res.facts) || {},
            }))
            .catch((err) => {
                _promise = null; // allow a retry on the next mount
                throw err;
            });
    }
    return _promise;
}

/**
 * Fetch the supported Odoo versions from the backend.
 *
 * @returns {Promise<string[]>} ordered version strings, e.g. ["7.0", …, "19.0"]
 */
export function fetchOdooVersions() {
    return fetchVersionInfo().then((info) => info.odoo_versions);
}

/**
 * Fetch each offered series' card, keyed by series.
 *
 * @returns {Promise<Object>} ``{"19.0": {postgres: ["14", …], postgres_default: "17"}, …}``
 */
export function fetchOdooVersionFacts() {
    return fetchVersionInfo().then((info) => info.facts);
}

/**
 * The key of a series in the facts, from however a form holds it.
 *
 * Forms keep ``odoo_version`` as a float (``19`` after ``parseFloat``);
 * the backend names series with one decimal (``"19.0"``).
 *
 * @param {number|string} version
 * @returns {string}
 */
export function seriesKey(version) {
    const n = parseFloat(version);
    return Number.isFinite(n) ? n.toFixed(1) : String(version || "");
}

/**
 * The PostgreSQL versions a series runs on, from the fetched facts.
 *
 * Falls back to every version any series runs on when the facts are not
 * in yet, or name no such series, so a picker is never empty.
 *
 * @param {Object} facts as ``fetchOdooVersionFacts`` resolves them
 * @param {number|string} version the series picked
 * @returns {string[]}
 */
export function pgVersionsFor(facts, version) {
    const card = facts && facts[seriesKey(version)];
    if (card && Array.isArray(card.postgres) && card.postgres.length) {
        return card.postgres;
    }
    const all = new Set();
    for (const other of Object.values(facts || {})) {
        for (const v of other.postgres || []) {
            all.add(v);
        }
    }
    return all.size ? [...all].sort((a, b) => Number(a) - Number(b)) : ["17"];
}

/**
 * The PostgreSQL version a new instance of a series gets.
 *
 * @param {Object} facts as ``fetchOdooVersionFacts`` resolves them
 * @param {number|string} version the series picked
 * @returns {string}
 */
export function pgDefaultFor(facts, version) {
    const card = facts && facts[seriesKey(version)];
    return (card && card.postgres_default) || "17";
}
