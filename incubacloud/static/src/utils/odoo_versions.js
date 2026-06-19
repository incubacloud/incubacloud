/** @odoo-module **/

import { rpc } from "@web/core/network/rpc";

let _promise = null;

/**
 * Fetch the supported Odoo versions from the backend.
 *
 * Single source of truth: the ``odoo_version`` Selection on the models
 * (served by ``/cloud/get_odoo_versions``). The result is cached for the
 * page lifetime so every picker shares a single request.
 *
 * @returns {Promise<string[]>} ordered version strings, e.g. ["7.0", …, "19.0"]
 */
export function fetchOdooVersions() {
    if (!_promise) {
        _promise = rpc("/cloud/get_odoo_versions")
            .then((res) => (res && res.odoo_versions) || [])
            .catch((err) => {
                _promise = null; // allow a retry on the next mount
                throw err;
            });
    }
    return _promise;
}
