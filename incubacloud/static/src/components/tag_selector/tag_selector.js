import { Component, useState } from "@odoo/owl";

// 10-color palette matching the `color` index stored on cloud.tag
export const TAG_COLORS = [
    { bg: "rgba(99,102,241,0.18)",  text: "#818CF8" },  // 0 indigo
    { bg: "rgba(139,92,246,0.18)",  text: "#A78BFA" },  // 1 violet
    { bg: "rgba(236,72,153,0.18)",  text: "#F472B6" },  // 2 pink
    { bg: "rgba(239,68,68,0.18)",   text: "#F87171" },  // 3 red
    { bg: "rgba(245,158,11,0.18)",  text: "#FCD34D" },  // 4 amber
    { bg: "rgba(34,197,94,0.18)",   text: "#4ADE80" },  // 5 green
    { bg: "rgba(59,130,246,0.18)",  text: "#60A5FA" },  // 6 blue
    { bg: "rgba(20,184,166,0.18)",  text: "#2DD4BF" },  // 7 teal
    { bg: "rgba(249,115,22,0.18)",  text: "#FB923C" },  // 8 orange
    { bg: "rgba(107,114,128,0.22)", text: "#9CA3AF" },  // 9 gray
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
