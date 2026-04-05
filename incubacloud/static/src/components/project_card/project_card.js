import { Component, useEnv } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { tagStyle, tagDotStyle } from "../tag_selector/tag_selector";

export class ProjectCard extends Component {
    static template = "incubacloud.ProjectCard";
    static props = {
        project: Object,
    };

    setup() {
        this.env = useEnv();
    }

    tagBadgeStyle(tag) { return tagStyle(tag); }
    tagDotStyle(tag)   { return tagDotStyle(tag); }

    statusLabel(status) {
        return { ok: _t("OK"), warning: _t("Warning"), error: _t("Error") }[status] || status;
    }

    onEnterProject() {
        const p = this.props.project;
        if (p.first_production_instance_id) {
            this.env.navigate("instance_detail", {
                project_id: p.id,
                instance_id: p.first_production_instance_id,
            });
        } else {
            this.env.navigate("project_detail", { project_id: p.id });
        }
    }
}
