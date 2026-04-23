import { Component, useState, onWillStart, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { TagSelector } from "../tag_selector/tag_selector";
import { useDialogA11y } from "../../utils/use_dialog_a11y";

export class CreateProjectModal extends Component {

    static components = { TagSelector };
    static template = "incubacloud.CreateProjectModal";

    static props = {
        onClose: Function,
        onCreated: Function,
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            name: "",
            description: "",
            selectedTags: [],
            allTags: [],
            loading: false,
            error: null,
        });
        this.data = useState(this.env.dialogData);
        this.boxRef = useRef("box");
        useDialogA11y(this.boxRef, () => this.close());

        onWillStart(() => this.loadTags());
    }

    async loadTags() {
        try {
            const res = await rpc("/cloud/get_tags");
            this.state.allTags = res.tags || res;
        } catch (e) {
            // tags are optional; swallow the error silently
        }
    }

    addTag(tag) {
        if (!this.state.selectedTags.find(t => t.id === tag.id)) {
            this.state.selectedTags.push(tag);
        }
    }

    removeTag(tagId) {
        const idx = this.state.selectedTags.findIndex(t => t.id === tagId);
        if (idx !== -1) this.state.selectedTags.splice(idx, 1);
    }

    async createTag(name) {
        const tag = await rpc("/cloud/create_tag", { name });
        if (!this.state.allTags.find(t => t.id === tag.id)) {
            this.state.allTags.push(tag);
        }
        return tag;
    }

    async close() {
        if (this.data.dismiss) {
            await this.data.dismiss();
        }
        if (this.props.onClose) {
            this.props.onClose();
        }
        return this.data.close({ dismiss: true });
    }

    get isValid() {
        return this.state.name.trim().length > 0;
    }

    onOverlayClick(ev) {
        if (ev.target.classList.contains("ic-modal-overlay")) {
            this.close();
        }
    }

    async create() {
        if (!this.isValid || this.state.loading) {
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        try {
            await this.orm.create("cloud.project", [{
                name: this.state.name.trim(),
                description: this.state.description || false,
                tag_ids: [[6, 0, this.state.selectedTags.map(t => t.id)]],
            }]);

            await this.close();
            this.props.onCreated?.();

        } catch (err) {
            this.state.error = err.message || "Failed to create project";
        } finally {
            this.state.loading = false;
        }
    }
}
