/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * Inline banner shown when a list endpoint truncates its result.
 * Rendered by any list page that reads a paginated endpoint so
 * operators know the UI doesn't show the full set.
 *
 * Usage:
 *   <TruncationBanner
 *       truncated="state.truncated"
 *       total="state.total"
 *       limit="state.limit"
 *       label="'projects'"/>
 */
export class TruncationBanner extends Component {
    static template = "incubacloud.TruncationBanner";
    static props = {
        truncated: { type: Boolean, optional: true },
        total: { type: Number, optional: true },
        limit: { type: Number, optional: true },
        label: { type: String, optional: true },
    };
}
