import { Component, useState } from "@odoo/owl";

// 10-color palette matching the `color` index stored on cloud.tag. The values
// are the --rl-tag-* tokens of relay.scss, one set per panel theme: a
// single set drew pale text on the light theme's paper (30-oct-2026).
export const TAG_COLORS = [
    { bg: "var(--rl-tag-0-bg)", text: "var(--rl-tag-0)" },  // 0 indigo
    { bg: "var(--rl-tag-1-bg)", text: "var(--rl-tag-1)" },  // 1 violet
    { bg: "var(--rl-tag-2-bg)", text: "var(--rl-tag-2)" },  // 2 pink
    { bg: "var(--rl-tag-3-bg)", text: "var(--rl-tag-3)" },  // 3 red
    { bg: "var(--rl-tag-4-bg)", text: "var(--rl-tag-4)" },  // 4 amber
    { bg: "var(--rl-tag-5-bg)", text: "var(--rl-tag-5)" },  // 5 green
    { bg: "var(--rl-tag-6-bg)", text: "var(--rl-tag-6)" },  // 6 blue
    { bg: "var(--rl-tag-7-bg)", text: "var(--rl-tag-7)" },  // 7 teal
    { bg: "var(--rl-tag-8-bg)", text: "var(--rl-tag-8)" },  // 8 orange
    { bg: "var(--rl-tag-9-bg)", text: "var(--rl-tag-9)" },  // 9 gray
];

export function tagStyle(tag) {
    const c = TAG_COLORS[(tag.color || 0) % TAG_COLORS.length];
    return `background:${c.bg};color:${c.text};`;
}

export function tagDotStyle(tag) {
    const c = TAG_COLORS[(tag.color || 0) % TAG_COLORS.length];
    return `background:${c.text};`;
}

/**
 * Reusable multi-tag selector.
 *
 * Props:
 *   selected  — [{id, name, color}]  currently selected tags
 *   available — [{id, name, color}]  all known tags
 *   onAdd     — (tag) => void
 *   onRemove  — (tagId) => void
 *   onCreate  — async (name) => tag object  (called when creating new tag)
 */
export class TagSelector extends Component {
    static template = "incubacloud.TagSelector";
    static props = {
        selected:  { type: Array },
        available: { type: Array },
        onAdd:     { type: Function },
        onRemove:  { type: Function },
        onCreate:  { type: Function },
    };

    setup() {
        this.state = useState({ input: "", open: false, creating: false });
    }

    get filtered() {
        const q = this.state.input.toLowerCase();
        const selectedIds = new Set(this.props.selected.map(t => t.id));
        return this.props.available.filter(
            t => !selectedIds.has(t.id) && (!q || t.name.toLowerCase().includes(q))
        );
    }

    get showCreate() {
        const q = this.state.input.trim();
        if (!q) return false;
        return !this.props.available.some(
            t => t.name.toLowerCase() === q.toLowerCase()
        );
    }

    onInput(ev) {
        this.state.input = ev.target.value;
        this.state.open = true;
    }

    onFocus() { this.state.open = true; }

    onBlur() {
        // Small delay so mousedown on dropdown option fires first
        setTimeout(() => { this.state.open = false; }, 160);
    }

    selectTag(tag) {
        this.props.onAdd(tag);
        this.state.input = "";
    }

    async createTag() {
        const name = this.state.input.trim();
        if (!name || this.state.creating) return;
        this.state.creating = true;
        try {
            const tag = await this.props.onCreate(name);
            this.selectTag(tag);
        } finally {
            this.state.creating = false;
        }
    }

    tagStyle(tag) { return tagStyle(tag); }
    tagDotStyle(tag) { return tagDotStyle(tag); }
}
