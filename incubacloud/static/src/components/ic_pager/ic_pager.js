import { Component } from "@odoo/owl";

/**
 * Reusable server-side paginator. Renders "from–to of total" plus prev/next
 * buttons and calls ``onPage(newOffset)`` when the user changes page. Renders
 * nothing when everything fits on one page (total <= limit).
 */
export class IcPager extends Component {
    static template = "incubacloud.IcPager";
    static props = {
        offset: { type: Number },
        limit: { type: Number },
        total: { type: Number },
        onPage: { type: Function },  // (newOffset: Number) => void
    };

    /** 1-based index of the first row shown (0 when empty). */
    get from() {
        return this.props.total === 0 ? 0 : this.props.offset + 1;
    }

    /** 1-based index of the last row shown. */
    get to() {
        return Math.min(this.props.offset + this.props.limit, this.props.total);
    }

    get canPrev() {
        return this.props.offset > 0;
    }

    get canNext() {
        return this.props.offset + this.props.limit < this.props.total;
    }

    /** Go to the previous page. */
    prev() {
        if (this.canPrev) {
            this.props.onPage(Math.max(0, this.props.offset - this.props.limit));
        }
    }

    /** Go to the next page. */
    next() {
        if (this.canNext) {
            this.props.onPage(this.props.offset + this.props.limit);
        }
    }
}
